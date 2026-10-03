import Foundation
import ImageIO

public struct RelayImageFrames {
    public let images: [CGImage]
    public let delays: [TimeInterval]
    public var duration: TimeInterval { delays.reduce(0, +) }

    public init(data: Data) throws {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { throw RelayError.invalidResponse }
        let type = CGImageSourceGetType(source) as String?
        let animated = ["com.compuserve.gif", "org.webmproject.webp", "public.png"].contains(type ?? "")
        let count = animated ? CGImageSourceGetCount(source) : 1
        guard count > 0, count <= 256 else { throw RelayError.responseTooLarge }
        var images: [CGImage] = []
        var delays: [TimeInterval] = []
        var decodedBytes = 0
        for index in 0..<count {
            guard let properties = CGImageSourceCopyPropertiesAtIndex(source, index, nil) as? [CFString: Any],
                  let width = properties[kCGImagePropertyPixelWidth] as? Int,
                  let height = properties[kCGImagePropertyPixelHeight] as? Int,
                  width > 0, height > 0, width <= 16_384, height <= 16_384, width <= 64_000_000 / height,
                  let image = CGImageSourceCreateThumbnailAtIndex(source, index, [kCGImageSourceCreateThumbnailFromImageAlways: true, kCGImageSourceThumbnailMaxPixelSize: count > 1 ? 512 : 2_048, kCGImageSourceCreateThumbnailWithTransform: true] as CFDictionary) else { throw RelayError.invalidResponse }
            decodedBytes += image.bytesPerRow * image.height
            // ponytail: Bound eager frame decoding; use streaming if larger animations need inline playback.
            guard decodedBytes <= 64 * 1_024 * 1_024 else { throw RelayError.responseTooLarge }
            images.append(image)
            let gif = properties[kCGImagePropertyGIFDictionary] as? [CFString: Any]
            let webp = properties[kCGImagePropertyWebPDictionary] as? [CFString: Any]
            let png = properties[kCGImagePropertyPNGDictionary] as? [CFString: Any]
            let delay = (gif?[kCGImagePropertyGIFUnclampedDelayTime] ?? gif?[kCGImagePropertyGIFDelayTime]
                ?? webp?[kCGImagePropertyWebPUnclampedDelayTime] ?? webp?[kCGImagePropertyWebPDelayTime]
                ?? png?[kCGImagePropertyAPNGUnclampedDelayTime] ?? png?[kCGImagePropertyAPNGDelayTime]) as? Double ?? 0.1
            delays.append(delay.isFinite && delay > 0 ? delay : 0.1)
        }
        self.images = images
        self.delays = delays
        guard duration.isFinite else { throw RelayError.invalidResponse }
    }
}
