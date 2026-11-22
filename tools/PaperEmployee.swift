import Foundation
import FoundationModels

struct Request: Decodable {
    struct Context: Decodable {
        let quoteMinorUnits: Int
        let exposureLimitMinorUnits: Int
    }
    let operation: String
    let context: Context?
}

struct ToolRequest: Encodable, Sendable {
    let tool = "paper_buy"
    let callId = UUID().uuidString
    let quantity: Int
    let limitMinorUnits: Int
}

actor Decision {
    private(set) var request: ToolRequest?

    func record(_ candidate: ToolRequest) -> Bool {
        guard request == nil else { return false }
        request = candidate
        return true
    }
}

struct PaperBuy: Tool {
    let name = "paper_buy"
    let description = "Request one simulated limit buy. No real funds are involved."
    let decision: Decision

    @Generable
    struct Arguments {
        @Guide(description: "Whole positive units to buy")
        let quantity: Int
        @Guide(description: "Positive per-unit limit price in minor currency units")
        let limitMinorUnits: Int
    }

    func call(arguments: Arguments) async throws -> String {
        let recorded = await decision.record(ToolRequest(
            quantity: arguments.quantity, limitMinorUnits: arguments.limitMinorUnits
        ))
        return recorded
            ? "Request staged for company checks; no fill has been confirmed."
            : "Only one order request is accepted in this duty."
    }
}

@main
struct PaperEmployee {
    static func emit<Value: Encodable>(_ value: Value) throws {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        FileHandle.standardOutput.write(try encoder.encode(value))
    }

    static func main() async {
        do {
            try await run()
        } catch {
            FileHandle.standardError.write(Data("Employee turn unavailable or invalid.\n".utf8))
            exit(1)
        }
    }

    static func run() async throws {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let request = try decoder.decode(
            Request.self, from: FileHandle.standardInput.readDataToEndOfFile()
        )
        let model = SystemLanguageModel.default
        let ready = model.availability == .available
        if request.operation == "ready" {
            try emit(["ready": ready])
            return
        }
        guard request.operation == "first_duty", ready, let context = request.context else {
            throw CocoaError(.featureUnsupported)
        }
        let decision = Decision()
        let session = LanguageModelSession(
            model: model,
            tools: [PaperBuy(decision: decision)],
            instructions: "You operate a bounded paper account. Decide whether to hold or request a small test trade. Never claim a fill."
        )
        _ = try await session.respond(to:
            "Fixed paper quote: \(context.quoteMinorUnits). Exposure cap: \(context.exposureLimitMinorUnits). Both are in minor currency units. Evaluate the setup and choose whether to hold or invoke the paper buy tool."
        )
        if let toolRequest = await decision.request {
            try emit(toolRequest)
        } else {
            try emit(["action": "hold"])
        }
    }
}
