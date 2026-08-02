import AppKit
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

// Build-time tool. Renders the app icon as vectors at every size macOS asks for and
// writes an .iconset directory; build.sh turns that into the .icns.
//
// The mark: a message bubble with a rising bar chart inside it — the two halves of what
// this app is. Colours are Apple's own system blue → indigo → purple.

struct Icon {

    static func draw(into ctx: CGContext, size: CGFloat) {
        let s = size
        ctx.setShouldAntialias(true)
        ctx.interpolationQuality = .high

        // macOS icons don't fill their canvas; the rounded square sits inset with a
        // soft shadow beneath it, on the standard Big Sur grid.
        let inset = s * 0.0879
        let rect = CGRect(x: inset, y: inset, width: s - inset * 2, height: s - inset * 2)
        let radius = rect.width * 0.2246

        let squircle = CGPath(
            roundedRect: rect, cornerWidth: radius, cornerHeight: radius, transform: nil)

        ctx.saveGState()
        ctx.setShadow(
            offset: CGSize(width: 0, height: -s * 0.012),
            blur: s * 0.035,
            color: CGColor(red: 0, green: 0, blue: 0, alpha: 0.26))
        ctx.addPath(squircle)
        ctx.setFillColor(CGColor(red: 0.04, green: 0.52, blue: 1.0, alpha: 1))
        ctx.fillPath()
        ctx.restoreGState()

        // Diagonal gradient: system blue into indigo into purple.
        ctx.saveGState()
        ctx.addPath(squircle)
        ctx.clip()
        let colors = [
            CGColor(red: 0.16, green: 0.63, blue: 1.00, alpha: 1),
            CGColor(red: 0.37, green: 0.36, blue: 0.90, alpha: 1),
            CGColor(red: 0.75, green: 0.35, blue: 0.95, alpha: 1),
        ] as CFArray
        if let gradient = CGGradient(
            colorsSpace: CGColorSpaceCreateDeviceRGB(),
            colors: colors,
            locations: [0.0, 0.55, 1.0]) {
            ctx.drawLinearGradient(
                gradient,
                start: CGPoint(x: rect.minX, y: rect.maxY),
                end: CGPoint(x: rect.maxX, y: rect.minY),
                options: [])
        }
        // A touch of light along the top edge keeps it from looking like flat plastic.
        if let sheen = CGGradient(
            colorsSpace: CGColorSpaceCreateDeviceRGB(),
            colors: [
                CGColor(red: 1, green: 1, blue: 1, alpha: 0.22),
                CGColor(red: 1, green: 1, blue: 1, alpha: 0.0),
            ] as CFArray,
            locations: [0.0, 0.45]) {
            ctx.drawLinearGradient(
                sheen,
                start: CGPoint(x: rect.midX, y: rect.maxY),
                end: CGPoint(x: rect.midX, y: rect.midY),
                options: [])
        }
        ctx.restoreGState()

        drawBubble(into: ctx, in: rect)
    }

    /// White speech bubble with a tail, holding three ascending bars.
    private static func drawBubble(into ctx: CGContext, in box: CGRect) {
        let w = box.width
        let bubble = CGRect(
            x: box.minX + w * 0.185,
            y: box.minY + w * 0.275,
            width: w * 0.63,
            height: w * 0.475)
        let radius = bubble.height * 0.30

        let path = CGMutablePath()
        path.addRoundedRect(
            in: bubble, cornerWidth: radius, cornerHeight: radius)

        // Tail, hanging off the lower-left corner.
        let tail = CGMutablePath()
        let tx = bubble.minX + bubble.width * 0.26
        let ty = bubble.minY
        tail.move(to: CGPoint(x: tx, y: ty + w * 0.055))
        tail.addCurve(
            to: CGPoint(x: tx - w * 0.105, y: ty - w * 0.098),
            control1: CGPoint(x: tx - w * 0.004, y: ty - w * 0.052),
            control2: CGPoint(x: tx - w * 0.042, y: ty - w * 0.082))
        tail.addCurve(
            to: CGPoint(x: tx + w * 0.108, y: ty + w * 0.020),
            control1: CGPoint(x: tx + w * 0.020, y: ty - w * 0.052),
            control2: CGPoint(x: tx + w * 0.072, y: ty - w * 0.006))
        tail.closeSubpath()

        ctx.saveGState()
        ctx.setShadow(
            offset: CGSize(width: 0, height: -w * 0.008),
            blur: w * 0.030,
            color: CGColor(red: 0.05, green: 0.10, blue: 0.35, alpha: 0.28))
        ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
        ctx.addPath(path)
        ctx.addPath(tail)
        ctx.fillPath()
        ctx.restoreGState()

        // Three bars climbing to the right — the "year in review" half of the idea.
        let barWidth = bubble.width * 0.135
        let gap = bubble.width * 0.088
        let baseline = bubble.minY + bubble.height * 0.235
        let heights: [CGFloat] = [0.235, 0.395, 0.545]
        let tints = [
            CGColor(red: 0.36, green: 0.68, blue: 1.00, alpha: 1),
            CGColor(red: 0.44, green: 0.42, blue: 0.93, alpha: 1),
            CGColor(red: 0.78, green: 0.38, blue: 0.96, alpha: 1),
        ]
        let totalWidth = barWidth * 3 + gap * 2
        var x = bubble.midX - totalWidth / 2

        for (index, factor) in heights.enumerated() {
            let bar = CGRect(
                x: x, y: baseline, width: barWidth, height: bubble.height * factor)
            let r = barWidth * 0.42
            ctx.addPath(
                CGPath(roundedRect: bar, cornerWidth: r, cornerHeight: r, transform: nil))
            ctx.setFillColor(tints[index])
            ctx.fillPath()
            x += barWidth + gap
        }
    }

    static func render(size: Int) -> CGImage? {
        guard let ctx = CGContext(
            data: nil,
            width: size,
            height: size,
            bitsPerComponent: 8,
            bytesPerRow: 0,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
        draw(into: ctx, size: CGFloat(size))
        return ctx.makeImage()
    }

    static func write(_ image: CGImage, to url: URL) throws {
        guard let dest = CGImageDestinationCreateWithURL(
            url as CFURL, UTType.png.identifier as CFString, 1, nil) else {
            throw NSError(domain: "IconGen", code: 1)
        }
        CGImageDestinationAddImage(dest, image, nil)
        guard CGImageDestinationFinalize(dest) else {
            throw NSError(domain: "IconGen", code: 2)
        }
    }
}

// MARK: - main

let args = CommandLine.arguments
guard args.count > 1 else {
    FileHandle.standardError.write(Data("usage: icongen <output.iconset>\n".utf8))
    exit(1)
}

let outputDir = URL(fileURLWithPath: args[1])
try? FileManager.default.createDirectory(at: outputDir, withIntermediateDirectories: true)

// The exact set `iconutil` expects.
let variants: [(name: String, pixels: Int)] = [
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
]

for variant in variants {
    guard let image = Icon.render(size: variant.pixels) else {
        FileHandle.standardError.write(Data("failed rendering \(variant.name)\n".utf8))
        exit(1)
    }
    try Icon.write(image, to: outputDir.appendingPathComponent(variant.name))
}

print("wrote \(variants.count) icon sizes to \(outputDir.path)")
