import Foundation
import ImageIO
import Testing
@testable import RelayCore

struct RelayImageFramesTests {
    @Test func decodesGIFFramesAndPreservesTheirTiming() throws {
        let data = NSMutableData()
        let destination = try #require(CGImageDestinationCreateWithData(data, "com.compuserve.gif" as CFString, 2, nil))
        for (index, delay) in [0.1, 0.25].enumerated() {
            let pixels = Data(index == 0 ? [255, 0, 0, 255] : [0, 0, 255, 255])
            let provider = try #require(CGDataProvider(data: pixels as CFData))
            let image = try #require(CGImage(width: 1, height: 1, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: 4, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.last.rawValue), provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent))
            CGImageDestinationAddImage(destination, image, [kCGImagePropertyGIFDictionary: [kCGImagePropertyGIFDelayTime: delay]] as CFDictionary)
        }
        #expect(CGImageDestinationFinalize(destination))
        let original = Data(referencing: data)
        let frames = try RelayImageFrames(data: original)
        #expect(frames.images.count == 2)
        #expect(abs(frames.delays[0] - 0.1) < 0.001)
        #expect(abs(frames.delays[1] - 0.25) < 0.001)
        #expect(frames.images[0].dataProvider?.data != frames.images[1].dataProvider?.data)
        #expect(Data(referencing: data) == original)
    }

    @Test func decodesAnimatedWebPFromItsActualBytes() throws {
        // Two lossless 2x2 frames, red then blue, with 100 ms and 250 ms durations.
        let bytes = try #require(Data(base64Encoded: "UklGRoQAAABXRUJQVlA4WAoAAAACAAAAAQAAAQAAQU5JTQYAAAAAAAAAAABBTk1GKAAAAAAAAAAAAAEAAAEAAGQAAAJWUDhMDwAAAC8BQAAABxD9j/4HIqL/AQBBTk1GKAAAAAAAAAAAAAEAAAEAAPoAAABWUDhMDwAAAC8BQAAABxDR//4HIqL/AQA="))
        let frames = try RelayImageFrames(data: bytes)
        #expect(frames.images.count == 2)
        #expect(abs(frames.duration - 0.35) < 0.001)
        #expect(frames.images[0].dataProvider?.data != frames.images[1].dataProvider?.data)
    }

    @Test func rejectsInvalidImageBytes() {
        #expect(throws: RelayError.invalidResponse) { try RelayImageFrames(data: Data("invalid".utf8)) }
    }
}
