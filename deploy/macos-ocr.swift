import Foundation
import Vision

let bytes = FileHandle.standardInput.readDataToEndOfFile()
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["ru-RU", "en-US"]
try VNImageRequestHandler(data: bytes).perform([request])

let fragments = (request.results ?? []).compactMap { observation -> (CGRect, String)? in
    guard let candidate = observation.topCandidates(1).first else { return nil }
    return (observation.boundingBox, candidate.string)
}.sorted { $0.0.midY > $1.0.midY }

var rows: [[(CGRect, String)]] = []
for fragment in fragments {
    if let first = rows.last?.first,
       abs(fragment.0.midY - first.0.midY) < min(fragment.0.height, first.0.height) * 0.6 {
        rows[rows.count - 1].append(fragment)
    } else {
        rows.append([fragment])
    }
}
for row in rows {
    print(row.sorted { $0.0.minX < $1.0.minX }.map { $0.1 }.joined(separator: " "))
}
