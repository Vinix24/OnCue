import CoreGraphics

/// Top-right origin of a window inside the visible frame of a screen.
/// AppKit coordinates: the origin is bottom-left, y grows upwards.
public func topRightOrigin(visibleFrame: CGRect, windowSize: CGSize, margin: CGFloat) -> CGPoint {
    CGPoint(
        x: visibleFrame.maxX - windowSize.width - margin,
        y: visibleFrame.maxY - windowSize.height - margin
    )
}
