import AppKit
import Foundation

// Original native vector artwork for Model Beacon. No downloaded images or fonts.
private let canvas: CGFloat = 1024

private func color(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: r, green: g, blue: b, alpha: a)
}

private func ovalGradient(_ rect: NSRect, _ colors: [NSColor]) {
    NSGraphicsContext.saveGraphicsState()
    NSBezierPath(ovalIn: rect).addClip()
    NSGradient(colors: colors)?.draw(from: NSPoint(x: rect.midX, y: rect.minY),
                                     to: NSPoint(x: rect.midX, y: rect.maxY), options: [])
    NSGraphicsContext.restoreGraphicsState()
}

private func radial(_ center: NSPoint, radius: CGFloat, from: NSColor, to: NSColor) {
    NSGradient(starting: from, ending: to)?.draw(fromCenter: center, radius: 0,
                                               toCenter: center, radius: radius,
                                               options: [.drawsAfterEndingLocation])
}

private func stroke(_ points: [NSPoint], width: CGFloat, ink: NSColor, close: Bool = false) {
    guard let first = points.first else { return }
    let path = NSBezierPath()
    path.move(to: first)
    for point in points.dropFirst() { path.line(to: point) }
    if close { path.close() }
    path.lineWidth = width
    path.lineCapStyle = .round
    path.lineJoinStyle = .round
    ink.setStroke()
    path.stroke()
}

private func drawBeacon(pixelSize: Int) {
    // A tapered tower, glazed lantern and six outward beams form the brand mark.
    // Small exports use stronger strokes and fewer internal details for legibility.
    let small = pixelSize <= 32
    let weight: CGFloat = small ? 37 : pixelSize <= 64 ? 25 : 18
    let ink = color(0.88, 0.95, 1)
    let rayInk = color(0.63, 0.82, 1, 0.90)

    NSGraphicsContext.saveGraphicsState()
    let glow = NSShadow()
    glow.shadowColor = color(0.24, 0.53, 0.88, small ? 0 : 0.18)
    glow.shadowBlurRadius = 14
    glow.shadowOffset = .zero
    glow.set()

    let tower = NSBezierPath()
    tower.move(to: NSPoint(x: 468, y: 390))
    tower.line(to: NSPoint(x: 434, y: 617))
    tower.line(to: NSPoint(x: 570, y: 617))
    tower.line(to: NSPoint(x: 536, y: 390))
    tower.close()
    color(0.60, 0.78, 0.96, 0.10).setFill()
    tower.fill()
    tower.lineWidth = weight
    tower.lineJoinStyle = .round
    ink.setStroke()
    tower.stroke()

    stroke([NSPoint(x: 441, y: 323), NSPoint(x: 502, y: 272), NSPoint(x: 563, y: 323)],
           width: weight, ink: ink)
    stroke([NSPoint(x: 449, y: 327), NSPoint(x: 449, y: 390),
            NSPoint(x: 555, y: 390), NSPoint(x: 555, y: 327)],
           width: weight, ink: ink)
    stroke([NSPoint(x: 436, y: 323), NSPoint(x: 568, y: 323)], width: weight, ink: ink)
    stroke([NSPoint(x: 416, y: 638), NSPoint(x: 588, y: 638)], width: weight, ink: ink)

    if !small {
        stroke([NSPoint(x: 502, y: 337), NSPoint(x: 502, y: 376)], width: weight * 0.65, ink: rayInk)
        stroke([NSPoint(x: 460, y: 484), NSPoint(x: 544, y: 484)], width: weight * 0.75, ink: ink)
        stroke([NSPoint(x: 447, y: 565), NSPoint(x: 557, y: 565)], width: weight * 0.75, ink: ink)
    }

    let rayWeight = small ? weight : weight * 0.82
    stroke([NSPoint(x: 350, y: 309), NSPoint(x: 414, y: 334)], width: rayWeight, ink: rayInk)
    stroke([NSPoint(x: 590, y: 334), NSPoint(x: 654, y: 309)], width: rayWeight, ink: rayInk)
    stroke([NSPoint(x: 330, y: 368), NSPoint(x: 406, y: 368)], width: rayWeight, ink: rayInk)
    stroke([NSPoint(x: 598, y: 368), NSPoint(x: 674, y: 368)], width: rayWeight, ink: rayInk)
    if !small {
        stroke([NSPoint(x: 350, y: 428), NSPoint(x: 414, y: 404)], width: rayWeight, ink: rayInk)
        stroke([NSPoint(x: 590, y: 404), NSPoint(x: 654, y: 428)], width: rayWeight, ink: rayInk)
    }
    NSGraphicsContext.restoreGraphicsState()
}

private func drawIcon(pixelSize: Int) {
    let orb = NSRect(x: 92, y: 64, width: 820, height: 820)
    NSGraphicsContext.saveGraphicsState()
    let shadow = NSShadow()
    shadow.shadowColor = color(0.035, 0.09, 0.18, 0.34)
    shadow.shadowBlurRadius = 70
    // A manually flipped bitmap context uses the opposite NSShadow vertical sign.
    shadow.shadowOffset = NSSize(width: 25, height: -48)
    shadow.set()
    color(0.08, 0.13, 0.20).setFill()
    NSBezierPath(ovalIn: orb).fill()
    NSGraphicsContext.restoreGraphicsState()

    ovalGradient(orb, [color(0.18, 0.25, 0.34), color(0.12, 0.18, 0.26), color(0.16, 0.22, 0.31)])
    ovalGradient(orb.insetBy(dx: 7.5, dy: 7.5),
                 [color(0.79, 0.855, 0.93), color(0.28, 0.40, 0.59), color(0.39, 0.52, 0.69)])
    ovalGradient(orb.insetBy(dx: 15.6, dy: 15.6), [color(0.11, 0.18, 0.28), color(0.08, 0.13, 0.21)])
    ovalGradient(orb.insetBy(dx: 17.8, dy: 17.8),
                 [color(0.565, 0.694, 0.898), color(0.298, 0.427, 0.655), color(0.329, 0.471, 0.651)])
    let face = orb.insetBy(dx: 32.4, dy: 32.4)
    ovalGradient(face, [color(0.13, 0.18, 0.255), color(0.090, 0.114, 0.153), color(0.070, 0.103, 0.145)])

    NSGraphicsContext.saveGraphicsState()
    NSBezierPath(ovalIn: face).addClip()
    radial(NSPoint(x: orb.minX + 164, y: orb.minY + 65.6), radius: 270.6,
           from: color(0.337, 0.498, 0.722, 0.75), to: color(0.337, 0.498, 0.722, 0))
    radial(NSPoint(x: orb.midX + 43, y: orb.maxY + 54), radius: 280,
           from: color(0.25, 0.39, 0.59, 0.50), to: color(0.25, 0.39, 0.59, 0))
    radial(NSPoint(x: 502, y: 365), radius: 135,
           from: color(0.20, 0.48, 0.80, 0.14), to: color(0.20, 0.48, 0.80, 0))
    NSGraphicsContext.restoreGraphicsState()
    drawBeacon(pixelSize: pixelSize)
}

private func png(size: Int) throws -> Data {
    guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
                                        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                        isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0),
          let context = NSGraphicsContext(bitmapImageRep: bitmap) else {
        throw NSError(domain: "ModelBeaconIcon", code: 1, userInfo: [NSLocalizedDescriptionKey: "Cannot create icon bitmap"])
    }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    let cg = context.cgContext
    cg.clear(CGRect(x: 0, y: 0, width: size, height: size))
    cg.translateBy(x: 0, y: CGFloat(size))
    cg.scaleBy(x: CGFloat(size) / canvas, y: -CGFloat(size) / canvas)
    cg.setShouldAntialias(true)
    drawIcon(pixelSize: size)
    NSGraphicsContext.restoreGraphicsState()
    guard let data = bitmap.representation(using: .png, properties: [:]) else {
        throw NSError(domain: "ModelBeaconIcon", code: 2, userInfo: [NSLocalizedDescriptionKey: "Cannot encode icon PNG"])
    }
    return data
}

let root = URL(fileURLWithPath: #filePath).standardizedFileURL.deletingLastPathComponent().deletingLastPathComponent()
let assets = root.appendingPathComponent("assets", isDirectory: true)
let iconset = root.appendingPathComponent(".build/AppIcon.iconset", isDirectory: true)
let fileManager = FileManager.default

do {
    try fileManager.createDirectory(at: assets, withIntermediateDirectories: true)
    if fileManager.fileExists(atPath: iconset.path) { try fileManager.removeItem(at: iconset) }
    try fileManager.createDirectory(at: iconset, withIntermediateDirectories: true)

    let entries = [
        ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
        ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
        ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
        ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
        ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024)
    ]
    var rendered: [Int: Data] = [:]
    for (name, size) in entries {
        let data: Data
        if let cached = rendered[size] { data = cached }
        else { data = try png(size: size); rendered[size] = data }
        try data.write(to: iconset.appendingPathComponent(name), options: .atomic)
    }
    try rendered[1024]!.write(to: assets.appendingPathComponent("AppIcon.png"), options: .atomic)

    let iconutil = Process()
    iconutil.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
    iconutil.arguments = ["-c", "icns", iconset.path, "-o", assets.appendingPathComponent("AppIcon.icns").path]
    try iconutil.run()
    iconutil.waitUntilExit()
    guard iconutil.terminationStatus == 0 else {
        throw NSError(domain: "ModelBeaconIcon", code: Int(iconutil.terminationStatus), userInfo: [NSLocalizedDescriptionKey: "iconutil failed"])
    }
    print("Generated assets/AppIcon.icns and assets/AppIcon.png")
} catch {
    fputs("Icon generation failed: \(error.localizedDescription)\n", stderr)
    exit(1)
}
