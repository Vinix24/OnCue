import Foundation

/// Formats device-change lines for stderr. Pure functions, so the format is testable.
///
/// Every line starts with `audiotee: device_change` and then carries stable
/// `key=value` pairs, so a person can grep a recorder.log for them. Names are
/// quoted with `"` and escaped; uids and numbers are plain tokens.
public enum DeviceChangeLog {
  public static let prefix = "audiotee: device_change"

  /// A device as the log shows it. `nil` stands for "no device" and logs as empty.
  public struct Device: Equatable {
    public let name: String
    public let uid: String

    public init(name: String, uid: String) {
      self.name = name
      self.uid = uid
    }
  }

  /// Escapes a value for use between double quotes on a single log line.
  public static func quote(_ value: String) -> String {
    var out = "\""
    for scalar in value.unicodeScalars {
      switch scalar {
      case "\\": out += "\\\\"
      case "\"": out += "\\\""
      case "\n": out += "\\n"
      case "\r": out += "\\r"
      case "\t": out += "\\t"
      default:
        if scalar.value < 0x20 || scalar.value == 0x7F {
          out += String(format: "\\u%04x", scalar.value)
        } else {
          out.unicodeScalars.append(scalar)
        }
      }
    }
    return out + "\""
  }

  /// A uid is a token without spaces; anything odd gets quoted.
  private static func uidToken(_ uid: String) -> String {
    let plain = !uid.isEmpty && uid.unicodeScalars.allSatisfy { $0.value > 0x20 && $0.value < 0x7F && $0 != "\"" && $0 != "\\" }
    return plain ? uid : quote(uid)
  }

  public static func timestamp(_ date: Date = Date()) -> String {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    formatter.timeZone = TimeZone(identifier: "UTC")
    return formatter.string(from: date)
  }

  /// `property` is one of `default_output`, `default_system_output`.
  public static func defaultDeviceChange(
    property: String, old: Device?, new: Device?, at date: Date = Date()
  ) -> String {
    return "\(prefix) ts=\(timestamp(date)) property=\(property)"
      + " old_name=\(quote(old?.name ?? "")) old_uid=\(uidToken(old?.uid ?? ""))"
      + " new_name=\(quote(new?.name ?? "")) new_uid=\(uidToken(new?.uid ?? ""))"
  }

  public static func deviceListChange(
    count: Int, added: [String], removed: [String], at date: Date = Date()
  ) -> String {
    return "\(prefix) ts=\(timestamp(date)) property=device_list count=\(count)"
      + " added=[\(added.map(quote).joined(separator: ","))]"
      + " removed=[\(removed.map(quote).joined(separator: ","))]"
  }

  public static func aliveChange(alive: Bool, at date: Date = Date()) -> String {
    return "\(prefix) ts=\(timestamp(date)) property=aggregate_alive alive=\(alive ? 1 : 0)"
  }

  /// The one line written at start, so a log always has the starting point.
  public static func startLine(
    output: Device?, systemOutput: Device?, at date: Date = Date()
  ) -> String {
    return "audiotee: device_start ts=\(timestamp(date))"
      + " output_name=\(quote(output?.name ?? "")) output_uid=\(uidToken(output?.uid ?? ""))"
      + " system_output_name=\(quote(systemOutput?.name ?? ""))"
      + " system_output_uid=\(uidToken(systemOutput?.uid ?? ""))"
  }

  public static func registrationFailure(property: String, status: Int32, at date: Date = Date())
    -> String
  {
    return "audiotee: device_change_unavailable ts=\(timestamp(date)) property=\(property) status=\(status)"
  }
}
