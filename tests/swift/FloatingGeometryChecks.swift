import Foundation
import CoreGraphics

@main
struct FloatingGeometryChecks {
    static var count = 0
    static func check(_ condition: @autoclosure () -> Bool, _ description: String) {
        count += 1
        guard condition() else { fatalError("Geometry regression: \(description)") }
    }
    static func near(_ a: CGFloat, _ b: CGFloat) -> Bool { abs(a - b) < 0.001 }
    static func contains(_ outer: CGRect, _ inner: CGRect) -> Bool {
        inner.minX >= outer.minX && inner.maxX <= outer.maxX && inner.minY >= outer.minY && inner.maxY <= outer.maxY
    }
    static func main() {
        let screen = CGRect(x: 0, y: 0, width: 1000, height: 750)
        let safe = FloatingGeometry.safeArea(screen)
        let orbSize = CGSize(width: 104, height: 104)
        let cardSize = CGSize(width: 344, height: 352)
        let left = CGRect(origin: CGPoint(x: 8, y: 400), size: orbSize)
        let right = CGRect(origin: CGPoint(x: 888, y: 400), size: orbSize)
        check(near(FloatingGeometry.expanded(around: left, size: cardSize, in: screen).minX, 8), "left edge opens inward")
        check(near(FloatingGeometry.expanded(around: right, size: cardSize, in: screen).maxX, 992), "right edge opens inward")
        for x: CGFloat in [8, 888] {
            for y: CGFloat in [8, 638] {
                let anchor = CGRect(origin: CGPoint(x: x, y: y), size: orbSize)
                for height: CGFloat in [352, 456, 390, 352] {
                    let card = FloatingGeometry.expanded(around: anchor, size: CGSize(width: 344, height: height), in: screen)
                    check(contains(safe, card), "corner card remains inside visible screen")
                    check(anchor.origin == CGPoint(x: x, y: y), "expand/collapse never mutates saved anchor")
                }
            }
        }
        let bottom = CGRect(x: 300, y: 8, width: 104, height: 104)
        check(near(FloatingGeometry.expanded(around: bottom, size: cardSize, in: screen).minY, 8), "bottom expands upward without moving orb")
        let top = CGRect(x: 300, y: 638, width: 104, height: 104)
        check(near(FloatingGeometry.expanded(around: top, size: cardSize, in: screen).maxY, 742), "top remains aligned")
        for (input, expected) in [(CGPoint(x: 22, y: 300), CGPoint(x: 8, y: 300)),
                                  (CGPoint(x: 875, y: 300), CGPoint(x: 888, y: 300)),
                                  (CGPoint(x: 300, y: 20), CGPoint(x: 300, y: 8)),
                                  (CGPoint(x: 300, y: 624), CGPoint(x: 300, y: 638)),
                                  (CGPoint(x: 300, y: 300), CGPoint(x: 300, y: 300))] {
            let snapped = FloatingGeometry.snapped(CGRect(origin: input, size: orbSize), to: screen)
            check(snapped.origin == expected, "each edge snaps; center remains free")
        }
        let secondScreen = CGRect(x: -1280, y: 0, width: 1280, height: 800)
        let externalAnchor = CGRect(x: -400, y: 200, width: 104, height: 104)
        check(FloatingGeometry.screen(for: externalAnchor, among: [screen, secondScreen]) == secondScreen, "negative-origin external display selected")
        let removedScreenAnchor = CGRect(x: 7000, y: 300, width: 104, height: 104)
        let restoredScreen = FloatingGeometry.screen(for: removedScreenAnchor, among: [screen, secondScreen])
        check(restoredScreen == screen, "removed display falls back to nearest available display")
        check(contains(safe, FloatingGeometry.constrained(removedScreenAnchor, to: restoredScreen)), "restored orb remains visible")
        let oldCard = FloatingGeometry.expanded(around: right, size: cardSize, in: screen)
        let movedCard = CGRect(x: 12, y: oldCard.minY, width: oldCard.width, height: oldCard.height)
        let movedAnchor = FloatingGeometry.movedOrb(right, previousPanel: oldCard, movedPanel: movedCard, visible: screen)
        check(near(movedAnchor.minX, 8), "dragging expanded card to left edge snaps its eventual orb to left edge")
        let overshotCard = movedCard.offsetBy(dx: -60, dy: -500)
        let overshotAnchor = FloatingGeometry.movedOrb(right, previousPanel: oldCard, movedPanel: overshotCard, visible: screen)
        check(near(overshotAnchor.minX, 8) && near(overshotAnchor.minY, 8), "overshooting a corner still snaps safely")
        var anchor = left
        for index in 0..<1000 {
            let card = FloatingGeometry.expanded(around: anchor, size: CGSize(width: 344, height: index % 2 == 0 ? 352 : 456), in: screen)
            check(contains(safe, card), "repeated dynamic expansion is visible")
            anchor = FloatingGeometry.constrained(anchor, to: screen)
        }
        check(anchor == left, "1000 expand/collapse cycles preserve exact anchor")

        for display in [screen, secondScreen, CGRect(x: 0, y: 0, width: 640, height: 480)] {
            let safeDisplay = FloatingGeometry.safeArea(display)
            let size = FloatingGeometry.panelSize(preferred: CGSize(width: 420, height: 724), in: display)
            check(size.width <= safeDisplay.width && size.height <= safeDisplay.height, "full panel fits small and external screens")
            let orb = FloatingGeometry.constrained(CGRect(origin: display.origin, size: orbSize), to: display)
            let card = FloatingGeometry.expanded(around: orb, size: size, in: display)
            check(contains(safeDisplay, card), "scrollable full panel remains entirely on screen")
        }
        print("\(count) geometry assertions passed")
    }
}
