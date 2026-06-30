import AppKit
import CoreGraphics
import Foundation

struct Mark {
    let x: CGFloat
    let y: CGFloat
    let label: String
}

struct Spec {
    let src: String
    let out: String
    let marks: [Mark]
}

let root = "/Users/nevrozq/Documents/AutoVPN/app/static/guides/amnezia"
let specs = [
    Spec(src: "\(root)/install-paste-key.jpg", out: "\(root)/install-paste-key-marked.jpg", marks: [
        Mark(x: 0.875, y: 0.288, label: "1"),
        Mark(x: 0.500, y: 0.383, label: "2"),
    ]),
    Spec(src: "\(root)/install-connect-confirm.jpg", out: "\(root)/install-connect-confirm-marked.jpg", marks: [
        Mark(x: 0.500, y: 0.365, label: "1"),
    ]),
    Spec(src: "\(root)/install-ready.jpg", out: "\(root)/install-ready-marked.jpg", marks: [
        Mark(x: 0.500, y: 0.375, label: "1"),
    ]),
    Spec(src: "\(root)/routing-enabled.jpg", out: "\(root)/update-open-servers-marked.jpg", marks: [
        Mark(x: 0.920, y: 0.813, label: "1"),
    ]),
    Spec(src: "\(root)/update-server-settings.jpg", out: "\(root)/update-server-settings-marked.jpg", marks: [
        Mark(x: 0.890, y: 0.492, label: "1"),
    ]),
    Spec(src: "\(root)/routing-enabled.jpg", out: "\(root)/routing-open-menu-marked.jpg", marks: [
        Mark(x: 0.795, y: 0.705, label: "1"),
    ]),
    Spec(src: "\(root)/routing-select-kind.jpg", out: "\(root)/routing-select-sites-marked.jpg", marks: [
        Mark(x: 0.920, y: 0.365, label: "1"),
    ]),
    Spec(src: "\(root)/routing-sites-import.jpg", out: "\(root)/routing-sites-import-marked.jpg", marks: [
        Mark(x: 0.920, y: 0.635, label: "1"),
    ]),
    Spec(src: "\(root)/routing-sites-replace.jpg", out: "\(root)/routing-sites-replace-marked.jpg", marks: [
        Mark(x: 0.610, y: 0.708, label: "1"),
    ]),
    Spec(src: "\(root)/routing-sites-ready.jpg", out: "\(root)/routing-sites-ready-marked.jpg", marks: [
        Mark(x: 0.905, y: 0.165, label: "1"),
    ]),
    Spec(src: "\(root)/routing-select-kind.jpg", out: "\(root)/routing-select-apps-marked.jpg", marks: [
        Mark(x: 0.920, y: 0.455, label: "1"),
    ]),
]

func annotate(_ spec: Spec) throws {
    guard let image = NSImage(contentsOfFile: spec.src) else {
        throw NSError(domain: "annotate", code: 1, userInfo: [NSLocalizedDescriptionKey: "Cannot open \(spec.src)"])
    }
    var proposedRect = NSRect(origin: .zero, size: image.size)
    guard let cgImage = image.cgImage(forProposedRect: &proposedRect, context: nil, hints: nil) else {
        throw NSError(domain: "annotate", code: 2, userInfo: [NSLocalizedDescriptionKey: "Cannot decode \(spec.src)"])
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
        throw NSError(domain: "annotate", code: 3, userInfo: [NSLocalizedDescriptionKey: "Cannot create bitmap"])
    }

    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    NSGraphicsContext.current?.cgContext.draw(cgImage, in: CGRect(x: 0, y: 0, width: width, height: height))

    let radius: CGFloat = CGFloat(width) * CGFloat(0.052)
    let lineWidth: CGFloat = max(CGFloat(width) * CGFloat(0.011), CGFloat(8))
    let paragraph = NSMutableParagraphStyle()
    paragraph.alignment = .center
    let textAttributes: [NSAttributedString.Key: Any] = [
        .font: NSFont.boldSystemFont(ofSize: radius * 0.9),
        .foregroundColor: NSColor(calibratedRed: 1, green: 0.969, blue: 0.933, alpha: 1),
        .paragraphStyle: paragraph,
    ]

    let showNumbers = spec.marks.count > 1
    for mark in spec.marks {
        let x: CGFloat = CGFloat(width) * mark.x
        let y: CGFloat = CGFloat(height) * (CGFloat(1) - mark.y)
        let outerRect = NSRect(x: x - radius, y: y - radius, width: radius * 2, height: radius * 2)
        let innerRadius = radius * 0.48
        let innerRect = NSRect(x: x - innerRadius, y: y - innerRadius, width: innerRadius * 2, height: innerRadius * 2)

        NSColor(calibratedRed: 0.984, green: 0.749, blue: 0.141, alpha: 0.24).setFill()
        NSColor(calibratedRed: 0.984, green: 0.749, blue: 0.141, alpha: 1).setStroke()
        let outerPath = NSBezierPath(ovalIn: outerRect)
        outerPath.lineWidth = lineWidth
        outerPath.fill()
        outerPath.stroke()

        if showNumbers {
            NSColor(calibratedRed: 0.067, green: 0.094, blue: 0.153, alpha: 1).setFill()
            NSBezierPath(ovalIn: innerRect).fill()

            let textRect = NSRect(x: x - radius, y: y - radius * 0.58, width: radius * 2, height: radius * 1.16)
            NSString(string: mark.label).draw(in: textRect, withAttributes: textAttributes)
        }
    }

    NSGraphicsContext.restoreGraphicsState()

    guard let data = rep.representation(
        using: NSBitmapImageRep.FileType.jpeg,
        properties: [NSBitmapImageRep.PropertyKey.compressionFactor: 0.9]
    ) else {
        throw NSError(domain: "annotate", code: 4, userInfo: [NSLocalizedDescriptionKey: "Cannot encode \(spec.out)"])
    }
    try data.write(to: URL(fileURLWithPath: spec.out), options: Data.WritingOptions.atomic)
    print(spec.out)
}

do {
    for spec in specs {
        try annotate(spec)
    }
} catch {
    fputs("\(error)\n", stderr)
    exit(1)
}
