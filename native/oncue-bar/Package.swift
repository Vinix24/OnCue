// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "oncue-bar",
    platforms: [.macOS("14.2")],
    targets: [
        .target(name: "OnCueBarCore"),
        .executableTarget(name: "OnCueBar", dependencies: ["OnCueBarCore"]),
        .testTarget(name: "OnCueBarCoreTests", dependencies: ["OnCueBarCore"]),
    ]
)
