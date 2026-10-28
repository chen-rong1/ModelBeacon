import Foundation
import CoreGraphics

// The orb owns the saved position. Expanded panels are derived from it and may
// be clamped to a screen without ever changing that anchor.
enum FloatingGeometry {
    static let screenInset: CGFloat = 8
    static let snapDistance: CGFloat = 24

    static func screen(for rect: CGRect, among screens: [CGRect]) -> CGRect {
        guard let first = screens.first else { return CGRect(x: 0, y: 0, width: 1000, height: 750) }
        let center = CGPoint(x: rect.midX, y: rect.midY)
        func score(_ bounds: CGRect) -> (CGFloat, CGFloat) {
            let overlap = bounds.intersection(rect)
            let area = overlap.isNull ? 0 : overlap.width * overlap.height
            let x = min(max(center.x, bounds.minX), bounds.maxX)
            let y = min(max(center.y, bounds.minY), bounds.maxY)
            return (area, -pow(center.x - x, 2) - pow(center.y - y, 2))
        }
        return screens.dropFirst().reduce(first) { best, next in
            let a = score(best), b = score(next)
            return b.0 > a.0 || (b.0 == a.0 && b.1 > a.1) ? next : best
        }
    }

    static func safeArea(_ visible: CGRect) -> CGRect {
        visible.insetBy(dx: min(screenInset, visible.width / 4), dy: min(screenInset, visible.height / 4))
    }

    static func panelSize(preferred: CGSize, in visible: CGRect) -> CGSize {
        let safe = safeArea(visible)
        return CGSize(width: min(preferred.width, safe.width), height: min(preferred.height, safe.height))
    }

    static func constrained(_ rect: CGRect, to visible: CGRect) -> CGRect {
        let safe = safeArea(visible)
        let x = min(max(rect.minX, safe.minX), max(safe.minX, safe.maxX - rect.width))
        let y = min(max(rect.minY, safe.minY), max(safe.minY, safe.maxY - rect.height))
        return CGRect(origin: CGPoint(x: x, y: y), size: rect.size)
    }

    static func snapped(_ rect: CGRect, to visible: CGRect) -> CGRect {
        let safe = safeArea(visible)
        var result = constrained(rect, to: visible)
        if min(abs(rect.minX - safe.minX), abs(rect.maxX - safe.maxX)) <= snapDistance {
            result.origin.x = abs(rect.minX - safe.minX) <= abs(rect.maxX - safe.maxX) ? safe.minX : safe.maxX - rect.width
        }
        if min(abs(rect.minY - safe.minY), abs(rect.maxY - safe.maxY)) <= snapDistance {
            result.origin.y = abs(rect.minY - safe.minY) <= abs(rect.maxY - safe.maxY) ? safe.minY : safe.maxY - rect.height
        }
        return result
    }

    static func expanded(around orb: CGRect, size: CGSize, in visible: CGRect) -> CGRect {
        // Open toward the side with more free space. Keep the orb's top edge
        // when possible, then clamp only the temporary card to the safe area.
        let safe = safeArea(visible)
        let right = safe.maxX - orb.minX
        let left = orb.maxX - safe.minX
        let x = right >= left ? orb.minX : orb.maxX - size.width
        return constrained(CGRect(x: x, y: orb.maxY - size.height, width: size.width, height: size.height), to: visible)
    }

    static func movedOrb(_ orb: CGRect, previousPanel: CGRect, movedPanel: CGRect, visible: CGRect) -> CGRect {
        let delta = CGPoint(x: movedPanel.minX - previousPanel.minX, y: movedPanel.minY - previousPanel.minY)
        var candidate = orb.offsetBy(dx: delta.x, dy: delta.y)
        let safe = safeArea(visible)
        // If a dragged card reaches an edge, its collapsed orb belongs to that
        // same edge rather than the card's former opposite corner.
        if movedPanel.minX <= safe.minX + snapDistance || movedPanel.maxX >= safe.maxX - snapDistance {
            candidate.origin.x = abs(movedPanel.minX - safe.minX) <= abs(movedPanel.maxX - safe.maxX) ? safe.minX : safe.maxX - orb.width
        }
        if movedPanel.minY <= safe.minY + snapDistance || movedPanel.maxY >= safe.maxY - snapDistance {
            candidate.origin.y = abs(movedPanel.minY - safe.minY) <= abs(movedPanel.maxY - safe.maxY) ? safe.minY : safe.maxY - orb.height
        }
        return snapped(candidate, to: visible)
    }
}

