"""Pixel check of the actual native source and served overlay, including inherited CSS."""
import subprocess
import tempfile
from pathlib import Path

from acp import overlay


SNAPSHOT = '''let snapshotTimer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { timer in
    guard companion.loaded, let view = companion.panel.contentView else { return }
    timer.invalidate()
    let window = companion.panel!
    let before = window.frame
    let collapsed = companion.prefs["collapsed"] as? Bool ?? false
    let points = [NSPoint(x: 18,y: 18), NSPoint(x: before.width-18,y: 18),
                  NSPoint(x: 18,y: before.height-18), NSPoint(x: before.width-18,y: before.height-18)]
    for point in points {
        precondition(view.hitTest(point) === companion.dragHandle, "Whole tile must receive gestures")
    }
    func event(_ type: NSEvent.EventType, _ screen: NSPoint) -> NSEvent {
        NSEvent.mouseEvent(with: type, location: window.convertPoint(fromScreen: screen),
            modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
            windowNumber: window.windowNumber, context: nil, eventNumber: 1, clickCount: 1, pressure: 1)!
    }
    let start = window.convertPoint(toScreen: NSPoint(x: before.width-30, y: before.height/2))
    let end = NSPoint(x: start.x-24, y: start.y+12)
    companion.dragHandle.mouseDown(with: event(.leftMouseDown,start))
    companion.dragHandle.mouseDragged(with: event(.leftMouseDragged,end))
    companion.dragHandle.mouseUp(with: event(.leftMouseUp,end))
    precondition(abs(window.frame.minX-before.minX+24)<1 && abs(window.frame.minY-before.minY-12)<1,
                 "Drag must move the native window")
    precondition((companion.prefs["collapsed"] as? Bool ?? false)==collapsed, "Drag must not toggle")
    // Leave room on the right/bottom so display-edge clamping is not involved.
    let visible = companion.screen()!.visibleFrame
    window.setFrameOrigin(NSPoint(x: visible.minX+80, y: visible.maxY-window.frame.height-80))
    let beforeResize = window.frame
    let grip = window.convertPoint(toScreen: NSPoint(x: window.frame.width-12,y: 12))
    companion.dragHandle.mouseDown(with: event(.leftMouseDown,grip))
    companion.dragHandle.mouseDragged(with: event(.leftMouseDragged,NSPoint(x: grip.x+36,y: grip.y-36)))
    companion.dragHandle.mouseDragged(with: event(.leftMouseDragged,NSPoint(x: grip.x+60,y: grip.y-60)))
    companion.dragHandle.mouseUp(with: event(.leftMouseUp,NSPoint(x: grip.x+60,y: grip.y-60)))
    precondition(abs(window.frame.width-beforeResize.width-60)<1, "Corner drag must resize")
    precondition(window.frame.width == window.frame.height, "Resize must stay square")
    precondition(abs(window.frame.minX-beforeResize.minX)<1 && abs(window.frame.maxY-beforeResize.maxY)<1,
                 "Resize must anchor the top-left corner")
    precondition((companion.prefs["collapsed"] as? Bool ?? false)==collapsed, "Resize must not toggle")
    precondition(companion.web.pageZoom > 1, "Content must scale with the panel")
    precondition(companion.readJSON("preferences.json")["displayScale"] as? Double == companion.displayScale,
                 "Resize scale must persist")
    let resizedScale = companion.displayScale
    let click = window.convertPoint(toScreen: NSPoint(x: 30,y: 45))
    companion.dragHandle.mouseDown(with: event(.leftMouseDown,click))
    companion.dragHandle.mouseUp(with: event(.leftMouseUp,click))
    DispatchQueue.main.asyncAfter(deadline: .now()+0.8) {
        precondition((companion.prefs["collapsed"] as? Bool ?? false) != collapsed, "Click must toggle")
        precondition(window.frame.width == window.frame.height, "Both sizes must be square")
        precondition(abs(companion.displayScale-resizedScale)<0.001, "Toggle must preserve chosen scale")
        // Force the toggle/drag race: update HTML and begin dragging before the
        // native 80ms debounce can apply the new layout.
        let currentMode = companion.prefs["collapsed"] as? Bool ?? false
        companion.web.evaluateJavaScript("activityOverlay.toggle()") { _, error in
            precondition(error == nil)
            let dragStart = window.convertPoint(toScreen: NSPoint(x: 30,y: 60))
            companion.dragHandle.mouseDown(with: event(.leftMouseDown,dragStart))
            companion.dragHandle.mouseDragged(with: event(.leftMouseDragged,NSPoint(x: dragStart.x+8,y: dragStart.y)))
            companion.dragHandle.mouseUp(with: event(.leftMouseUp,NSPoint(x: dragStart.x+8,y: dragStart.y)))
            DispatchQueue.main.asyncAfter(deadline: .now()+0.8) {
                precondition((companion.prefs["collapsed"] as? Bool ?? false) != currentMode,
                             "Immediate drag must retain the pending toggle")
                let base: Double = currentMode ? 224 : 180
                precondition(abs(window.frame.width-base*resizedScale)<1, "DOM and native size must agree")
        guard let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { exit(2) }
        view.cacheDisplay(in: view.bounds, to: bitmap)
        let corner = bitmap.colorAt(x: 1, y: 1)!.alphaComponent
        let fill = bitmap.colorAt(x: bitmap.pixelsWide-8, y: bitmap.pixelsHigh/2)!.alphaComponent
        print("Native gestures passed; surface alpha: corner=\\(corner), fill=\\(fill)")
        exit(corner < 0.05 && fill > 0.2 && fill < 0.9 ? 0 : 1)
            }
        }
    }
}
DispatchQueue.main.asyncAfter(deadline: .now()+20) { exit(2) }
app.run()'''



def check_surface(url):
    # Compile the production views unchanged, adding only a test capture at startup.
    # Captures this app's view hierarchy, not the user's desktop or other windows.
    with tempfile.TemporaryDirectory(prefix='acp-surface-') as tmp:
        folder = Path(tmp)
        source = Path(overlay.__file__).parent / 'native' / 'Leo.swift'
        harness = folder / 'surface.swift'
        harness.write_text(source.read_text().replace('app.run()', SNAPSHOT))
        executable = folder / 'surface-check'
        subprocess.run(['swiftc', str(harness), '-o', str(executable),
                        '-framework', 'AppKit', '-framework', 'WebKit'], check=True, timeout=120)
        (folder / 'control.token').write_text('isolated-surface-check')
        subprocess.run([str(executable), str(folder), url], check=True, timeout=25)
