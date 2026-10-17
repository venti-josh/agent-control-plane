import AppKit
import WebKit
import Darwin

let notificationName = Notification.Name("com.acp.leo.control")
let arguments = CommandLine.arguments
if arguments.count == 5 && arguments[1] == "--control" {
    let folder = URL(fileURLWithPath: arguments[2], isDirectory: true)
    guard let token = try? String(contentsOf: folder.appendingPathComponent("control.token"), encoding: .utf8) else { exit(2) }
    DistributedNotificationCenter.default().postNotificationName(notificationName, object: token,
        userInfo: ["action": arguments[3], "request_id": arguments[4]], deliverImmediately: true)
    exit(0)
}
guard arguments.count == 3, let origin = URL(string: arguments[2]), origin.scheme == "http",
      ["127.0.0.1", "localhost", "::1"].contains(origin.host ?? "") else { exit(2) }

final class LeoPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

// One rounded surface owns the fill and edge. A visual-effect material can add
// an opaque window-server backdrop outside the content's layer mask.
final class OverlaySurface: NSView {
    override var isOpaque: Bool { false }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.clear.setFill()
        bounds.fill(using: .copy)
        let shape = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: 19.5, yRadius: 19.5)
        NSColor(calibratedRed: 1, green: 0.988, blue: 0.969, alpha: 0.66).setFill()
        shape.fill()
    }
    override func resizeSubviews(withOldSize oldSize: NSSize) {
        super.resizeSubviews(withOldSize: oldSize)
        needsDisplay = true
        window?.invalidateShadow()
    }
}

// AppKit receives the original mouse event, so dragging works outside the web view
// and across displays without a delayed JavaScript-to-native mouse event bridge.
final class DragHandle: NSView {
    weak var owner: Companion?
    var pressPoint: NSPoint?
    var pressOrigin = NSPoint.zero
    var moved = false
    var resizing = false
    var pressSize: CGFloat = 0
    var pressTop: CGFloat = 0
    var resizeArea: NSRect { NSRect(x: bounds.maxX-26, y: 0, width: 26, height: 26) }
    override func draw(_ dirtyRect: NSRect) {
        NSColor(calibratedWhite: 0.35, alpha: 0.4).setStroke()
        let grip = NSBezierPath()
        grip.lineWidth = 1.5
        grip.lineCapStyle = .round
        for offset in [CGFloat(0), CGFloat(5)] {
            grip.move(to: NSPoint(x: bounds.maxX-17+offset, y: 8))
            grip.line(to: NSPoint(x: bounds.maxX-8, y: 17-offset))
        }
        grip.stroke()
    }
    override func resizeSubviews(withOldSize oldSize: NSSize) {
        super.resizeSubviews(withOldSize: oldSize)
        needsDisplay = true
        window?.invalidateCursorRects(for: self)
    }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func mouseDown(with event: NSEvent) {
        guard let window = window else { return }
        pressPoint = window.convertPoint(toScreen: event.locationInWindow)
        pressOrigin = window.frame.origin
        pressSize = window.frame.width
        pressTop = window.frame.maxY
        resizing = resizeArea.contains(convert(event.locationInWindow, from: nil))
        owner?.resizeWork?.cancel()
        moved = false
    }
    override func mouseDragged(with event: NSEvent) {
        guard let window = window, let start = pressPoint else { return }
        let point = window.convertPoint(toScreen: event.locationInWindow)
        let dx = point.x-start.x, dy = point.y-start.y
        if hypot(dx,dy) >= 4 { moved = true }
        if moved {
            owner?.dragging = true
            if resizing {
                owner?.resize(to: pressSize+(dx-dy)/2, left: pressOrigin.x, top: pressTop)
            } else {
                window.setFrameOrigin(NSPoint(x: pressOrigin.x+dx, y: pressOrigin.y+dy))
            }
        }
    }
    override func mouseUp(with event: NSEvent) {
        guard pressPoint != nil else { return }
        pressPoint = nil
        owner?.dragging = false
        if moved { owner?.constrain(); owner?.save() }
        else if !resizing { owner?.toggle() }
        resizing = false
        // A toggle can change the DOM just before a drag cancels its debounced
        // native resize. Ask the page for its final layout after every gesture.
        owner?.web.evaluateJavaScript("window.dispatchEvent(new Event('resize'))", completionHandler: nil)
    }
    override func resetCursorRects() {
        addCursorRect(bounds, cursor: .openHand)
        addCursorRect(resizeArea, cursor: .crosshair)
    }
}

final class Companion: NSObject, NSApplicationDelegate, NSWindowDelegate, WKScriptMessageHandler, WKNavigationDelegate {
    let folder: URL
    let origin: URL
    var panel: LeoPanel!
    var web: WKWebView!
    var dragHandle: DragHandle!
    var item: NSStatusItem!
    var lockFD: Int32 = -1
    var observer: NSObjectProtocol?
    var timer: Timer?
    var prefs: [String: Any] = [:]
    var metrics: [String: Any] = [:]
    var loaded = false
    var pageReady = false
    var resizeWork: DispatchWorkItem?
    var dragging = false
    var displayScale = 1.0
    var lastRequest = ""
    var loadError = ""

    init(folder: URL, origin: URL) { self.folder = folder; self.origin = origin }
    func readJSON(_ name: String) -> [String: Any] {
        guard let data = try? Data(contentsOf: folder.appendingPathComponent(name)),
              let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return value
    }
    func writeJSON(_ name: String, _ value: [String: Any]) {
        if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) {
            try? data.write(to: folder.appendingPathComponent(name), options: .atomic)
        }
    }
    func applicationDidFinishLaunching(_ notification: Notification) {
        lockFD = open(folder.appendingPathComponent("panel.lock").path, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
        guard lockFD >= 0, flock(lockFD, LOCK_EX | LOCK_NB) == 0 else { NSApp.terminate(nil); return }
        prefs = readJSON("preferences.json")
        let configuration = WKWebViewConfiguration()
        configuration.userContentController.add(self, name: "leoOverlay")
        let collapsed = (prefs["collapsed"] as? Bool) ?? false
        configuration.userContentController.addUserScript(WKUserScript(
            source: "window.leoInitialCollapsed = \(collapsed ? "true" : "false");",
            injectionTime: .atDocumentStart, forMainFrameOnly: true))
        if let savedScale = prefs["displayScale"] as? Double, savedScale.isFinite {
            displayScale = min(2, max(0.8, savedScale))
        }
        let initialHeight: Double = (collapsed ? 180 : 224) * displayScale
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: initialHeight, height: initialHeight), configuration: configuration)
        web.pageZoom = displayScale
        web.navigationDelegate = self
        web.underPageBackgroundColor = .clear
        // macOS WebKit otherwise paints an opaque backing even for transparent HTML.
        web.setValue(false, forKey: "drawsBackground")
        web.wantsLayer = true
        web.layer?.cornerRadius = 20
        web.layer?.masksToBounds = true
        web.autoresizingMask = [.width, .height]
        panel = LeoPanel(contentRect: web.frame, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.title = "ACP Comet"
        panel.isReleasedWhenClosed = false
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        if #available(macOS 13.0, *) { panel.collectionBehavior.insert(.canJoinAllApplications) }
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        // Keep native drag hit-testing outside WebKit's flipped view hierarchy.
        let surface = OverlaySurface(frame: web.frame)
        surface.wantsLayer = true
        surface.layer?.backgroundColor = NSColor.clear.cgColor
        surface.layer?.cornerRadius = 20
        surface.layer?.masksToBounds = true
        panel.contentView = surface
        surface.addSubview(web)
        panel.delegate = self
        panel.isExcludedFromWindowsMenu = true
        dragHandle = DragHandle(frame: surface.bounds)
        dragHandle.owner = self
        dragHandle.autoresizingMask = [.width, .height]
        surface.addSubview(dragHandle, positioned: .above, relativeTo: web)
        if let x = prefs["x"] as? Double, let y = prefs["y"] as? Double { panel.setFrameOrigin(NSPoint(x: x, y: y)) }
        else { resetPosition() }
        constrain()
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "☄️"
        item.button?.setAccessibilityLabel("ACP Comet")
        item.button?.toolTip = "Comet · ACP agent activity"
        let menu = NSMenu()
        for (title, action) in [("Show Comet", #selector(show)), ("Hide Comet", #selector(hide)),
                                ("Collapse / Expand", #selector(toggle)), ("Reset position", #selector(reset))] {
            let entry = menu.addItem(withTitle: title, action: action, keyEquivalent: "")
            entry.target = self
        }
        menu.addItem(.separator())
        menu.addItem(withTitle: "Open ACP board", action: #selector(openBoard), keyEquivalent: "").target = self
        menu.addItem(withTitle: "Quit Comet", action: #selector(quit), keyEquivalent: "").target = self
        item.menu = menu
        guard let token = try? String(contentsOf: folder.appendingPathComponent("control.token"), encoding: .utf8) else { quit(); return }
        observer = DistributedNotificationCenter.default().addObserver(forName: notificationName, object: token, queue: .main) { [weak self] note in
            guard let self = self, let action = note.userInfo?["action"] as? String else { return }
            self.lastRequest = note.userInfo?["request_id"] as? String ?? ""
            switch action {
            case "show": self.show()
            case "hide": self.hide()
            case "toggle": self.toggle()
            case "reset": self.reset()
            case "quit": self.quit()
            default: return
            }
            self.writeState()
        }
        NotificationCenter.default.addObserver(self, selector: #selector(screensChanged), name: NSApplication.didChangeScreenParametersNotification, object: nil)
        web.load(URLRequest(url: origin.appendingPathComponent("overlay")))
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.writeState() }
        writeState()
    }
    func screen() -> NSScreen? {
        NSScreen.screens.max { first, second in
            let a = first.visibleFrame.intersection(panel.frame), b = second.visibleFrame.intersection(panel.frame)
            return a.width * a.height < b.width * b.height
        } ?? NSScreen.main
    }
    func resize(to size: CGFloat, left: CGFloat, top: CGFloat) {
        let base: Double = (prefs["collapsed"] as? Bool ?? false) ? 180 : 224
        displayScale = min(2, max(0.8, size/base))
        if let visible = screen()?.visibleFrame {
            displayScale = min(displayScale, max(100, min(visible.width, visible.height)-24)/base)
        }
        web.pageZoom = displayScale
        let side = base * displayScale
        panel.setFrame(NSRect(x: left, y: top-side, width: side, height: side), display: true)
        prefs["displayScale"] = displayScale
    }
    func constrain() {
        guard !dragging, let visible = screen()?.visibleFrame else { return }
        var frame = panel.frame
        let limit = max(100, min(visible.width, visible.height)-24)
        if frame.width > limit {
            displayScale *= limit/frame.width
            web.pageZoom = displayScale
            frame.size = NSSize(width: limit, height: limit)
        }
        frame.origin.x = min(max(frame.minX, visible.minX + 12), visible.maxX - frame.width - 12)
        frame.origin.y = min(max(frame.minY, visible.minY + 12), visible.maxY - frame.height - 12)
        if frame != panel.frame { panel.setFrame(frame, display: true) }
    }
    func resetPosition() {
        guard let screen = panel?.screen ?? NSScreen.main else { return }
        panel.setFrameOrigin(NSPoint(x: screen.visibleFrame.maxX - panel.frame.width - 18, y: screen.visibleFrame.minY + 18))
    }
    @objc func screensChanged() { constrain(); save() }
    @objc func show() { prefs["hidden"] = false; constrain(); panel.orderFrontRegardless(); web.evaluateJavaScript("cometPet.setVisible(true)", completionHandler: nil); save() }
    @objc func hide() { prefs["hidden"] = true; panel.orderOut(nil); web.evaluateJavaScript("cometPet.setVisible(false)", completionHandler: nil); save() }
    @objc func toggle() { web.evaluateJavaScript("activityOverlay.toggle()", completionHandler: nil) }
    @objc func reset() { resetPosition(); constrain(); save() }
    @objc func openBoard() { NSWorkspace.shared.open(origin) }
    @objc func quit() { NSApp.terminate(nil) }
    func windowDidMove(_ notification: Notification) { if !dragging { save() } }
    func save() {
        guard panel != nil else { return }
        prefs["displayScale"] = displayScale
        prefs["x"] = panel.frame.minX; prefs["y"] = panel.frame.minY; prefs["height"] = panel.frame.height
        writeJSON("preferences.json", prefs)
        writeState()
    }
    func writeState() {
        guard panel != nil else { return }
        let frame = panel.frame
        let point = NSPoint(x: 80, y: panel.contentView!.bounds.height - 16)
        let hit = panel.contentView?.hitTest(panel.contentView?.superview?.convert(point, from: nil) ?? point)
        writeJSON("state.json", ["pid": ProcessInfo.processInfo.processIdentifier, "url": origin.absoluteString,
            "visible": panel.isVisible, "on_screen": panel.occlusionState.contains(.visible), "loaded": loaded,
            "window_id": panel.windowNumber, "level": panel.level.rawValue,
            "all_spaces": panel.collectionBehavior.contains(.canJoinAllSpaces),
            "full_screen_auxiliary": panel.collectionBehavior.contains(.fullScreenAuxiliary),
            "nonactivating": panel.styleMask.contains(.nonactivatingPanel), "hides_on_deactivate": panel.hidesOnDeactivate,
            "frontmost_app": NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? "",
            "frame": ["x": frame.minX, "y": frame.minY, "width": frame.width, "height": frame.height],
            "display_scale": displayScale, "resize_grip": true,
            "collapsed": prefs["collapsed"] as? Bool ?? false, "request_id": lastRequest,
            "metrics": metrics, "error": loadError,
            "heading_draggable": hit === dragHandle,
            "heading_hit_view": hit.map { String(describing: type(of: $0)) } ?? "none",
            "web_flipped": web.isFlipped,
            "web_background_alpha": web.underPageBackgroundColor.alphaComponent,
            "web_draws_background": web.value(forKey: "drawsBackground") as? Bool ?? true])
    }
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame, message.frameInfo.request.url?.host == origin.host,
              let body = message.body as? [String: Any], let action = body["action"] as? String else { return }
        switch action {
        case "ready": pageReady = true
        case "resize":
            guard !dragging else { return }
            if let height = body["height"] as? Double, let width = body["width"] as? Double,
               height.isFinite, width.isFinite, height >= 100, height <= 800, width >= 100, width <= 400 {
                // WebKit can report a transient wrapped layout before its final width settles.
                // Coalesce those observations before constraining the native frame to a display.
                resizeWork?.cancel()
                let collapsed = body["collapsed"] as? Bool ?? false
                let work = DispatchWorkItem { [weak self] in
                    guard let self = self else { return }
                    guard !self.dragging else { return }
                    let top = self.panel.frame.maxY
                    let height = height * self.displayScale, width = width * self.displayScale
                    if self.panel.frame.height != height || self.panel.frame.width != width {
                        self.panel.setFrame(NSRect(x: self.panel.frame.minX, y: top - height, width: width, height: height), display: true)
                    }
                    self.prefs["collapsed"] = collapsed
                    self.constrain(); self.save()
                    if self.pageReady && !self.loaded {
                        self.loaded = true; self.loadError = ""
                        if !(self.prefs["hidden"] as? Bool ?? false) { self.show() }
                    }
                    self.writeState()
                }
                resizeWork = work
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.08, execute: work)
            }
        case "hide": hide()
        case "move":
            if let dx = body["dx"] as? Double, let dy = body["dy"] as? Double,
               dx.isFinite, dy.isFinite, abs(dx) <= 100, abs(dy) <= 100 {
                panel.setFrameOrigin(NSPoint(x: panel.frame.minX + dx, y: panel.frame.minY - dy)); constrain(); save()
            }
        case "reset": reset()
        case "metrics": metrics = body["data"] as? [String: Any] ?? [:]
        default: break
        }
        writeState()
    }
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let url = navigationAction.request.url
        decisionHandler(url?.scheme == origin.scheme && url?.host == origin.host && url?.port == origin.port && url?.path == "/overlay" ? .allow : .cancel)
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        loadError = error.localizedDescription; writeState()
    }
    func applicationWillTerminate(_ notification: Notification) {
        timer?.invalidate()
        if let observer = observer { DistributedNotificationCenter.default().removeObserver(observer) }
        if lockFD >= 0 { close(lockFD) }
    }
}

let app = NSApplication.shared
let companion = Companion(folder: URL(fileURLWithPath: arguments[1], isDirectory: true), origin: origin)
app.setActivationPolicy(.accessory)
app.delegate = companion
app.run()
