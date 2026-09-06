import AppKit
import Darwin
import Foundation

private func option(_ name: String) -> String? {
    let arguments = CommandLine.arguments
    var index = 1
    while index + 1 < arguments.count {
        if arguments[index] == name {
            return arguments[index + 1]
        }
        index += 2
    }
    return nil
}

private func positiveInteger(_ name: String) -> Int? {
    guard let raw = option(name), let value = Int(raw), value > 0 else {
        return nil
    }
    return value
}

private func appendProbe(path: String?, event: String) {
    guard let path, !path.isEmpty, let data = "\(event)\n".data(using: .utf8) else {
        return
    }
    let fileManager = FileManager.default
    if !fileManager.fileExists(atPath: path) {
        fileManager.createFile(atPath: path, contents: nil)
    }
    guard let handle = FileHandle(forWritingAtPath: path) else {
        return
    }
    handle.seekToEndOfFile()
    handle.write(data)
    handle.closeFile()
}

private func themeColor(_ red: Int, _ green: Int, _ blue: Int) -> NSColor {
    NSColor(
        srgbRed: CGFloat(red) / 255,
        green: CGFloat(green) / 255,
        blue: CGFloat(blue) / 255,
        alpha: 1
    )
}

private func imageView(path: String, frame: NSRect) -> NSImageView? {
    guard let image = NSImage(contentsOfFile: path) else {
        return nil
    }
    let view = NSImageView(frame: frame)
    view.image = image
    view.imageScaling = .scaleProportionallyUpOrDown
    return view
}

private func label(
    _ text: String,
    frame: NSRect,
    font: NSFont,
    color: NSColor
) -> NSTextField {
    let field = NSTextField(labelWithString: text)
    field.frame = frame
    field.alignment = .center
    field.font = font
    field.textColor = color
    field.lineBreakMode = .byWordWrapping
    field.maximumNumberOfLines = 2
    return field
}

guard
    let delayMilliseconds = positiveInteger("--delay-ms"),
    let timeoutSeconds = positiveInteger("--timeout-seconds"),
    let readyPath = option("--ready-path")
else {
    exit(2)
}

let contentWidth: CGFloat = 520
let contentHeight: CGFloat = 390
let fileManager = FileManager.default
let probePath = option("--probe-path")
let headlessProbe = option("--probe-mode") == "headless"
appendProbe(path: probePath, event: "started")
let showAt = Date().addingTimeInterval(Double(delayMilliseconds) / 1000.0)
while Date() < showAt {
    if fileManager.fileExists(atPath: readyPath) {
        appendProbe(path: probePath, event: "suppressed-ready")
        exit(0)
    }
    Thread.sleep(forTimeInterval: 0.1)
}

if headlessProbe {
    appendProbe(path: probePath, event: "shown")
    let timeoutAt = Date().addingTimeInterval(Double(timeoutSeconds))
    while !fileManager.fileExists(atPath: readyPath) && Date() < timeoutAt {
        Thread.sleep(forTimeInterval: 0.1)
    }
    appendProbe(
        path: probePath,
        event: fileManager.fileExists(atPath: readyPath) ? "ready" : "timeout"
    )
    exit(0)
}

guard
    let markPath = option("--mark-path"),
    let wordmarkPath = option("--wordmark-path"),
    let mark = imageView(path: markPath, frame: NSRect(x: 228, y: 308, width: 64, height: 64)),
    let wordmark = imageView(path: wordmarkPath, frame: NSRect(x: 180, y: 257, width: 160, height: 39))
else {
    exit(3)
}

let application = NSApplication.shared
application.setActivationPolicy(.accessory)

let window = NSWindow(
    contentRect: NSRect(x: 0, y: 0, width: contentWidth, height: contentHeight),
    styleMask: [.titled],
    backing: .buffered,
    defer: false
)
window.title = "BreakTwenty is updating"
window.level = .floating
window.isMovable = true
window.center()

let contentView = NSView(frame: NSRect(x: 0, y: 0, width: contentWidth, height: contentHeight))
contentView.wantsLayer = true
contentView.layer?.backgroundColor = themeColor(24, 24, 23).cgColor
window.contentView = contentView
contentView.addSubview(mark)
contentView.addSubview(wordmark)

let spinner = NSProgressIndicator(frame: NSRect(x: 245, y: 54, width: 30, height: 30))
spinner.style = .spinning
spinner.isIndeterminate = true
spinner.isDisplayedWhenStopped = true
contentView.addSubview(spinner)

contentView.addSubview(label(
    "Please wait, BreakTwenty is updating",
    frame: NSRect(x: 28, y: 207, width: 464, height: 32),
    font: NSFont.boldSystemFont(ofSize: 16),
    color: themeColor(237, 231, 221)
))

if let version = option("--version"), !version.isEmpty {
    contentView.addSubview(label(
        "Updating to BreakTwenty \(version)",
        frame: NSRect(x: 28, y: 181, width: 464, height: 24),
        font: NSFont.systemFont(ofSize: 12),
        color: themeColor(142, 135, 123)
    ))
}

contentView.addSubview(label(
    "BreakTwenty will reopen automatically when the update is finished.",
    frame: NSRect(x: 65, y: 124, width: 390, height: 48),
    font: NSFont.systemFont(ofSize: 13),
    color: themeColor(232, 188, 146)
))

let timeoutAt = Date().addingTimeInterval(Double(timeoutSeconds))
Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { timer in
    let ready = fileManager.fileExists(atPath: readyPath)
    if ready || Date() >= timeoutAt {
        appendProbe(path: probePath, event: ready ? "ready" : "timeout")
        timer.invalidate()
        application.terminate(nil)
    }
}

spinner.startAnimation(nil)
window.makeKeyAndOrderFront(nil)
application.activate(ignoringOtherApps: true)
appendProbe(path: probePath, event: "shown")
application.run()
