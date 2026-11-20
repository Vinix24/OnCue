import AppKit
import Carbon.HIToolbox
import OnCueBarCore
import os
import WebKit

let bundleId = Bundle.main.bundleIdentifier ?? "nl.vnxdigital.oncue.bar"
let log = Logger(subsystem: bundleId, category: "bar")

let gripWidth: CGFloat = 10
let webSize = CGSize(width: 340, height: 56)
let panelSize = CGSize(width: gripWidth + webSize.width, height: webSize.height)
let margin: CGFloat = 12

func parseArguments() -> (port: Int, hotkey: String) {
    var port: Int?
    var hotkey = "ctrl+option+r"
    var args = CommandLine.arguments.dropFirst().makeIterator()
    while let arg = args.next() {
        switch arg {
        case "--port": port = args.next().flatMap { Int($0) }
        case "--hotkey": hotkey = args.next() ?? hotkey
        default: break
        }
    }
    guard let port, (1...65535).contains(port) else {
        FileHandle.standardError.write(Data("OnCue Bar: --port <n> is required and must be a number\n".utf8))
        exit(2)
    }
    return (port, hotkey)
}

/// Bar texts from the server's own catalog (`/api/config` names the language, `/i18n/<lang>.json`
/// holds the strings), the same source the bar page reads. English until the catalog arrives.
final class BarTexts {
    private let lock = NSLock()
    private var strings: [String: String] = [:]

    func text(_ key: String, fallback: String) -> String {
        lock.lock(); defer { lock.unlock() }
        return strings[key] ?? fallback
    }

    func fetch(port: Int) {
        let base = "http://127.0.0.1:\(port)"
        var request = URLRequest(url: URL(string: "\(base)/api/config")!)
        request.timeoutInterval = 3
        URLSession.shared.dataTask(with: request) { [weak self] data, _, _ in
            guard let data,
                  let config = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let language = config["language"] as? String,
                  language.allSatisfy({ $0.isLetter }),
                  let url = URL(string: "\(base)/i18n/\(language).json") else { return }
            var catalogRequest = URLRequest(url: url)
            catalogRequest.timeoutInterval = 3
            URLSession.shared.dataTask(with: catalogRequest) { data, _, _ in
                guard let data,
                      let catalog = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let recorder = catalog["recorder"] as? [String: String] else { return }
                self?.lock.lock()
                self?.strings = recorder
                self?.lock.unlock()
            }.resume()
        }.resume()
    }
}

let barTexts = BarTexts()

func quitMenu(target: AnyObject, action: Selector) -> NSMenu {
    let menu = NSMenu()
    let item = NSMenuItem(title: barTexts.text("bar_quit", fallback: "Quit OnCue"), action: action, keyEquivalent: "")
    item.target = target
    menu.addItem(item)
    return menu
}

/// Drag handle: moves the window without activating the app.
final class GripView: NSView {
    var onRightClick: ((NSEvent, NSView) -> Void)?
    override func mouseDown(with event: NSEvent) { window?.performDrag(with: event) }
    override func rightMouseDown(with event: NSEvent) { onRightClick?(event, self) }
    override func draw(_ dirtyRect: NSRect) {
        NSColor(calibratedWhite: 0.09, alpha: 1).setFill()
        bounds.fill()
        NSColor(calibratedWhite: 0.45, alpha: 1).setFill()
        for dy in stride(from: -8 as CGFloat, through: 8, by: 8) {
            for dx in [-2 as CGFloat, 2] {
                NSBezierPath(ovalIn: NSRect(x: bounds.midX + dx - 1, y: bounds.midY + dy - 1, width: 2, height: 2)).fill()
            }
        }
    }
}

final class BarWebView: WKWebView {
    var quitMenuProvider: (() -> NSMenu)?
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func willOpenMenu(_ menu: NSMenu, with event: NSEvent) {
        guard let custom = quitMenuProvider?() else { return }
        menu.removeAllItems()
        for item in custom.items {
            custom.removeItem(item)
            menu.addItem(item)
        }
    }
}

final class BarController: NSObject, WKNavigationDelegate {
    let port: Int
    let panel: NSPanel
    let webView: BarWebView
    var retryScheduled = false

    var barURL: URL { URL(string: "http://127.0.0.1:\(port)/bar")! }

    init(port: Int) {
        self.port = port
        webView = BarWebView(frame: NSRect(x: gripWidth, y: 0, width: webSize.width, height: webSize.height))
        panel = NSPanel(
            contentRect: NSRect(origin: .zero, size: panelSize),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        super.init()
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = true
        panel.level = .statusBar
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.hasShadow = true

        let content = NSView(frame: NSRect(origin: .zero, size: panelSize))
        let grip = GripView(frame: NSRect(x: 0, y: 0, width: gripWidth, height: webSize.height))
        content.addSubview(grip)
        let makeMenu = { [unowned self] in quitMenu(target: self, action: #selector(self.quitOnCue)) }
        grip.onRightClick = { event, view in NSMenu.popUpContextMenu(makeMenu(), with: event, for: view) }
        webView.quitMenuProvider = makeMenu
        webView.navigationDelegate = self
        webView.setValue(false, forKey: "drawsBackground")
        content.addSubview(webView)
        panel.contentView = content

        // The screen with the menu bar is the first one; NSScreen.main follows keyboard focus.
        let screen = menuBarScreenFrame(
            visibleFrames: NSScreen.screens.map { $0.visibleFrame },
            fallback: NSRect(x: 0, y: 0, width: 1440, height: 900)
        )
        panel.setFrameOrigin(topRightOrigin(visibleFrame: screen, windowSize: panelSize, margin: margin))
        panel.orderFrontRegardless()
        load()
    }

    func load() {
        retryScheduled = false
        barTexts.fetch(port: port)
        webView.load(URLRequest(url: barURL))
    }

    func showLost() {
        log.error("loading \(self.barURL.absoluteString, privacy: .public) failed, retrying in 2 s")
        webView.loadHTMLString(lostPage(), baseURL: nil)
        guard !retryScheduled else { return }
        retryScheduled = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in self?.load() }
    }

    func isBarURL(_ url: URL) -> Bool {
        url.scheme == "http" && url.host == "127.0.0.1" && url.port == port && url.path == "/bar"
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor action: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = action.request.url else { decisionHandler(.cancel); return }
        if action.targetFrame == nil || action.navigationType == .linkActivated {
            if url.scheme == "http" || url.scheme == "https" { NSWorkspace.shared.open(url) }
            else { log.error("blocked navigation to \(url.absoluteString, privacy: .public)") }
            decisionHandler(.cancel)
        } else if isBarURL(url) || url.absoluteString == "about:blank" {
            decisionHandler(.allow)
        } else {
            log.error("blocked navigation to \(url.absoluteString, privacy: .public)")
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        showLost()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        showLost()
    }

    func webView(_ webView: WKWebView, webContentProcessDidTerminate: WKWebView) { showLost() }

    @objc func quitOnCue() {
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(port)/status")!)
        request.timeoutInterval = 2
        URLSession.shared.dataTask(with: request) { data, response, _ in
            let ok = (response as? HTTPURLResponse)?.statusCode == 200
            let status = ok ? data.flatMap(parseServerStatus) : nil
            DispatchQueue.main.async { Self.finishQuit(plan: quitPlan(for: status)) }
        }.resume()
    }

    static func finishQuit(plan: QuitPlan) {
        if let question = plan.question {
            NSApp.activate(ignoringOtherApps: true)
            let alert = NSAlert()
            alert.messageText = question
            alert.addButton(withTitle: "Afsluiten")
            alert.addButton(withTitle: "Annuleren")
            guard alert.runModal() == .alertFirstButtonReturn else { return }
        }
        if let pid = plan.signalPid, pid > 1 {
            if kill(pid_t(pid), SIGTERM) != 0 {
                log.error("SIGTERM to server pid \(pid) failed (errno \(errno))")
            }
        }
        NSApp.terminate(nil)
    }

    func toggleRecording() {
        webView.evaluateJavaScript("window.oncueBar && window.oncueBar.toggle()") { _, error in
            if let error { log.error("hotkey toggle failed: \(error.localizedDescription, privacy: .public)") }
        }
    }
}

func lostPage() -> String {
    let text = barTexts.text("bar_connection_lost", fallback: "connection lost")
    return """
<!DOCTYPE html><meta charset="utf-8"><style>
html,body{margin:0;overflow:hidden;background:#0d0f14}
div{width:340px;height:56px;box-sizing:border-box;border:1px solid #242a36;display:flex;align-items:center;
padding:0 10px;color:#8b94a7;font:12px -apple-system,system-ui,sans-serif;
background:repeating-linear-gradient(45deg,#161a22 0 8px,#242a36 8px 16px)}
</style><div>\(text)</div>
"""
}

var controller: BarController?

func registerHotkey(_ spec: String) {
    do {
        let parsed = try parseHotkey(spec)
        var eventType = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        let installed = InstallEventHandler(GetApplicationEventTarget(), { _, _, _ in
            DispatchQueue.main.async { controller?.toggleRecording() }
            return noErr
        }, 1, &eventType, nil, nil)
        var ref: EventHotKeyRef?
        let id = EventHotKeyID(signature: OSType(0x4F43_4252), id: 1)
        let status = RegisterEventHotKey(parsed.keyCode, parsed.modifiers, id, GetApplicationEventTarget(), 0, &ref)
        if installed != noErr || status != noErr {
            log.error("hotkey registration failed (handler \(installed), key \(status)), running without hotkey")
        }
    } catch {
        log.error("invalid hotkey '\(spec, privacy: .public)': \(String(describing: error), privacy: .public), running without hotkey")
    }
}

let options = parseArguments()
let app = NSApplication.shared
signal(SIGTERM, SIG_IGN)
let termSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
termSource.setEventHandler { NSApp.terminate(nil) }
termSource.resume()
app.setActivationPolicy(.accessory)
controller = BarController(port: options.port)
registerHotkey(options.hotkey)
app.run()
