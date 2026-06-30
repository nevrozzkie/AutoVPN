import AppKit
import Foundation

struct Redaction {
    let x: CGFloat
    let y: CGFloat
    let width: CGFloat
    let height: CGFloat
}

struct RedactionSpec {
    let file: String
    let redactions: [Redaction]
}

let root = "/Users/nevrozq/Documents/AutoVPN/app/static/guides/amnezia"
let specs = [
    RedactionSpec(file: "\(root)/install-ready.jpg", redactions: [
        Redaction(x: 0.56, y: 0.870, width: 0.38, height: 0.045),
    ]),
    RedactionSpec(file: "\(root)/routing-enabled.jpg", redactions: [
        Redaction(x: 0.56, y: 0.870, width: 0.38, height: 0.045),
    ]),
    RedactionSpec(file: "\(root)/update-server-settings.jpg", redactions: [
        Redaction(x: 0.47, y: 0.220, width: 0.36, height: 0.045),
        Redaction(x: 0.18, y: 0.495, width: 0.38, height: 0.045),
    ]),
    RedactionSpec(file: "\(root)/routing-sites-import.jpg", redactions: [
        Redaction(x: 0.03, y: 0.378, width: 0.45, height: 0.050),
        Redaction(x: 0.03, y: 0.466, width: 0.47, height: 0.050),
    ]),
    RedactionSpec(file: "\(root)/routing-sites-replace.jpg", redactions: [
        Redaction(x: 0.03, y: 0.378, width: 0.45, height: 0.050),
        Redaction(x: 0.03, y: 0.466, width: 0.47, height: 0.050),
    ]),
    RedactionSpec(file: "\(root)/routing-sites-ready.jpg", redactions: [
        Redaction(x: 0.03, y: 0.378, width: 0.45, height: 0.050),
        Redaction(x: 0.03, y: 0.466, width: 0.47, height: 0.050),
        Redaction(x: 0.03, y: 0.553, width: 0.47, height: 0.050),
        Redaction(x: 0.03, y: 0.640, width: 0.47, height: 0.050),
        Redaction(x: 0.03, y: 0.728, width: 0.47, height: 0.050),
    ]),
]

func redact(_ spec: RedactionSpec) throws {
    guard let image = NSImage(contentsOfFile: spec.file) else {
        throw NSError(domain: "redact", code: 1, userInfo: [NSLocalizedDescriptionKey: "Cannot open \(spec.file)"])
    }
    var proposedRect = NSRect(origin: .zero, size: image.size)
    guard let cgImage = image.cgImage(forProposedRect: &proposedRect, context: nil, hints: nil) else {
        throw NSError(domain: "redact", code: 2, userInfo: [NSLocalizedDescriptionKey: "Cannot decode \(spec.file)"])
    }

    let width = cgImage.width
    let height = cgImage.height
    guard let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: width,
        pixelsHigh: height,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    ) else {
        throw NSError(domain: "redact", code: 3, userInfo: [NSLocalizedDescriptionKey: "Cannot create bitmap"])
    }

    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    guard let context = NSGraphicsContext.current?.cgContext else {
        throw NSError(domain: "redact", code: 4, userInfo: [NSLocalizedDescriptionKey: "Cannot create context"])
    }
    context.draw(cgImage, in: CGRect(x: 0, y: 0, width: width, height: height))

    NSColor(calibratedRed: 0.055, green: 0.063, blue: 0.078, alpha: 1).setFill()
    for item in spec.redactions {
        let rect = NSRect(
            x: CGFloat(width) * item.x,
            y: CGFloat(height) * (CGFloat(1) - item.y - item.height),
            width: CGFloat(width) * item.width,
            height: CGFloat(height) * item.height
        )
        let path = NSBezierPath(roundedRect: rect, xRadius: 8, yRadius: 8)
        path.fill()
    }
    NSGraphicsContext.restoreGraphicsState()

    guard let data = rep.representation(
        using: NSBitmapImageRep.FileType.jpeg,
        properties: [NSBitmapImageRep.PropertyKey.compressionFactor: 0.9]
    ) else {
        throw NSError(domain: "redact", code: 5, userInfo: [NSLocalizedDescriptionKey: "Cannot encode \(spec.file)"])
    }
    try data.write(to: URL(fileURLWithPath: spec.file), options: Data.WritingOptions.atomic)
    print(spec.file)
}

do {
    for spec in specs {
        try redact(spec)
    }
} catch {
    fputs("\(error)\n", stderr)
    exit(1)
}
