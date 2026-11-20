import CoreAudio
import Foundation

/// Logs output-device and route changes to stderr. Log only: the listeners never
/// touch the IO proc, the tap or the aggregate device. A listener that fails to
/// register is logged once and ignored; capture carries on as before.
public final class DeviceChangeMonitor {
  private struct Registration {
    let objectID: AudioObjectID
    var address: AudioObjectPropertyAddress
    let block: AudioObjectPropertyListenerBlock
  }

  private let aggregateDeviceID: AudioObjectID
  private let queue = DispatchQueue(label: "audiotee.device-change-monitor")
  private let emit: (String) -> Void
  private var registrations: [Registration] = []
  // Only touched on `queue`.
  private var defaultOutput: DeviceChangeLog.Device?
  private var defaultSystemOutput: DeviceChangeLog.Device?
  private var knownDevices: [AudioObjectID: String] = [:]
  private var started = false

  public init(
    aggregateDeviceID: AudioObjectID,
    emit: @escaping (String) -> Void = DeviceChangeMonitor.writeToStderr
  ) {
    self.aggregateDeviceID = aggregateDeviceID
    self.emit = emit
  }

  deinit { stop() }

  public static func writeToStderr(_ line: String) {
    FileHandle.standardError.write(Data((line + "\n").utf8))
  }

  public func start() {
    queue.sync {
      guard !started else { return }
      started = true
      let system = AudioObjectID(kAudioObjectSystemObject)
      defaultOutput = Self.readDefault(kAudioHardwarePropertyDefaultOutputDevice)
      defaultSystemOutput = Self.readDefault(kAudioHardwarePropertyDefaultSystemOutputDevice)
      knownDevices = Self.readDeviceNames()
      emit(DeviceChangeLog.startLine(output: defaultOutput, systemOutput: defaultSystemOutput))

      register(system, kAudioHardwarePropertyDefaultOutputDevice, "default_output") { [weak self] in
        self?.handleDefault(
          kAudioHardwarePropertyDefaultOutputDevice, "default_output", \.defaultOutput)
      }
      register(system, kAudioHardwarePropertyDefaultSystemOutputDevice, "default_system_output") {
        [weak self] in
        self?.handleDefault(
          kAudioHardwarePropertyDefaultSystemOutputDevice, "default_system_output",
          \.defaultSystemOutput)
      }
      register(system, kAudioHardwarePropertyDevices, "device_list") { [weak self] in
        self?.handleDeviceList()
      }
      register(aggregateDeviceID, kAudioDevicePropertyDeviceIsAlive, "aggregate_alive") {
        [weak self] in self?.handleAlive()
      }
    }
  }

  public func stop() {
    queue.sync {
      for var reg in registrations {
        AudioObjectRemovePropertyListenerBlock(reg.objectID, &reg.address, queue, reg.block)
      }
      registrations.removeAll()
      started = false
    }
  }

  private func register(
    _ objectID: AudioObjectID, _ selector: AudioObjectPropertySelector, _ name: String,
    _ handler: @escaping () -> Void
  ) {
    var address = getPropertyAddress(selector: selector)
    let block: AudioObjectPropertyListenerBlock = { _, _ in handler() }
    let status = AudioObjectAddPropertyListenerBlock(objectID, &address, queue, block)
    if status == kAudioHardwareNoError {
      registrations.append(Registration(objectID: objectID, address: address, block: block))
    } else {
      emit(DeviceChangeLog.registrationFailure(property: name, status: status))
    }
  }

  // MARK: - Handlers (run on `queue`)

  private func handleDefault(
    _ selector: AudioObjectPropertySelector, _ name: String,
    _ keyPath: ReferenceWritableKeyPath<DeviceChangeMonitor, DeviceChangeLog.Device?>
  ) {
    let new = Self.readDefault(selector)
    let old = self[keyPath: keyPath]
    guard new != old else { return }
    self[keyPath: keyPath] = new
    emit(DeviceChangeLog.defaultDeviceChange(property: name, old: old, new: new))
  }

  private func handleDeviceList() {
    let current = Self.readDeviceNames()
    let added = current.filter { knownDevices[$0.key] == nil }.map { $0.value }.sorted()
    let removed = knownDevices.filter { current[$0.key] == nil }.map { $0.value }.sorted()
    knownDevices = current
    emit(DeviceChangeLog.deviceListChange(count: current.count, added: added, removed: removed))
  }

  private func handleAlive() {
    var address = getPropertyAddress(selector: kAudioDevicePropertyDeviceIsAlive)
    var alive: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    let status = AudioObjectGetPropertyData(aggregateDeviceID, &address, 0, nil, &size, &alive)
    // A failed read means the device is gone or unreadable: log it as not alive.
    emit(DeviceChangeLog.aliveChange(alive: status == kAudioHardwareNoError && alive == 1))
  }

  // MARK: - CoreAudio reads

  private static func readDefault(_ selector: AudioObjectPropertySelector)
    -> DeviceChangeLog.Device?
  {
    var address = getPropertyAddress(selector: selector)
    var id = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let status = AudioObjectGetPropertyData(
      AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &id)
    guard status == kAudioHardwareNoError, id != kAudioObjectUnknown else { return nil }
    return DeviceChangeLog.Device(
      name: readString(id, kAudioObjectPropertyName) ?? "",
      uid: readString(id, kAudioDevicePropertyDeviceUID) ?? "")
  }

  private static func readDeviceNames() -> [AudioObjectID: String] {
    var address = getPropertyAddress(selector: kAudioHardwarePropertyDevices)
    var size: UInt32 = 0
    let system = AudioObjectID(kAudioObjectSystemObject)
    guard AudioObjectGetPropertyDataSize(system, &address, 0, nil, &size) == kAudioHardwareNoError
    else { return [:] }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(system, &address, 0, nil, &size, &ids) == kAudioHardwareNoError
    else { return [:] }
    var out: [AudioObjectID: String] = [:]
    for id in ids { out[id] = readString(id, kAudioObjectPropertyName) ?? "id\(id)" }
    return out
  }

  private static func readString(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector)
    -> String?
  {
    var address = getPropertyAddress(selector: selector)
    var value: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    let status = AudioObjectGetPropertyData(id, &address, 0, nil, &size, &value)
    guard status == kAudioHardwareNoError, let value = value else { return nil }
    return value.takeRetainedValue() as String
  }
}
