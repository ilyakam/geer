import AppKit
import SwiftUI

private let setupCommand = "/usr/local/bin/geer"
private let fallbackCommand = "/Library/Application Support/Geer/app/geer-setup.command"

private struct GeerLogo: View {
    private let image: NSImage? = {
        guard let url = Bundle.main.url(forResource: "Geer", withExtension: "svg") else {
            return nil
        }
        return NSImage(contentsOf: url)
    }()

    var body: some View {
        Group {
            if let image {
                Image(nsImage: image)
                    .resizable()
                    .interpolation(.high)
            } else {
                Color.clear
            }
        }
        .aspectRatio(1, contentMode: .fit)
        .accessibilityLabel("Geer")
    }
}

private struct SetupFeatureRow: View {
    let text: String
    let systemImage: String

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            Image(systemName: systemImage)
                .frame(width: 22, height: 22, alignment: .center)
            Text(text)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

private struct SetupDetailsView: View {
    let lines: [String]
    @Binding var isExpanded: Bool

    var body: some View {
        GeometryReader { geometry in
            VStack(alignment: .leading, spacing: 8) {
                Button {
                    isExpanded.toggle()
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: "chevron.right")
                            .font(.caption.weight(.semibold))
                            .rotationEffect(.degrees(isExpanded ? 90 : 0))
                        Text("Setup details")
                            .font(.headline)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityValue(isExpanded ? "Expanded" : "Collapsed")

                if isExpanded {
                    ScrollViewReader { proxy in
                        ScrollView(.vertical) {
                            Text(lines.joined(separator: "\n"))
                                .font(.system(.caption, design: .monospaced))
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .id("setup-log-end")
                        }
                        .frame(
                            width: geometry.size.width,
                            height: max(geometry.size.height - 30, 0),
                            alignment: .top
                        )
                        .onChange(of: lines.count) { _ in
                            proxy.scrollTo("setup-log-end", anchor: .bottom)
                        }
                        .onAppear {
                            proxy.scrollTo("setup-log-end", anchor: .bottom)
                        }
                    }
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .frame(minHeight: 30, maxHeight: .infinity)
        .layoutPriority(1)
    }
}

@MainActor
final class SetupViewModel: ObservableObject {
    enum State {
        case welcome, running, ready, cancelled, failed
    }

    @Published var state: State = .welcome
    @Published var headline = "Set up private, local coding"
    @Published var detail = "Geer will prepare its local engine, reuse any verified model already on this Mac, connect repository search, and optionally configure T3 Code."
    @Published var phaseTitle = "Ready to begin"
    @Published var completed = 0.0
    @Published var total = 5.0
    @Published var logLines: [String] = []
    @Published var decisionPrompt: String?
    @Published var defaultDecision = "yes"
    @Published var retainedModel = false
    @Published var modelName = "Selected for this Mac when setup begins"
    @Published var download = "Calculated before download"
    @Published var contextWindow = "Calculated for this Mac"
    @Published var kvCache = "BF16"
    @Published var requiredSpace = "Calculated before download"

    private var process: Process?
    private var input: FileHandle?
    private var stdoutBuffer = Data()
    private var pendingDecisionID: String?

    func begin() {
        guard process == nil else { return }
        state = .running
        headline = "Preparing Geer"
        detail = "You can safely cancel and relaunch setup. Verified model files are retained."

        let process = Process()
        let stdout = Pipe()
        let stderr = Pipe()
        let stdin = Pipe()
        process.executableURL = URL(fileURLWithPath: setupCommand)
        process.arguments = ["setup", "--frontend", "json-v1"]
        process.standardOutput = stdout
        process.standardError = stderr
        process.standardInput = stdin
        self.process = process
        self.input = stdin.fileHandleForWriting

        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            Task { @MainActor in self?.consume(data) }
        }
        stderr.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard let text = String(data: data, encoding: .utf8), !text.isEmpty else { return }
            Task { @MainActor in self?.appendLog(text) }
        }
        process.terminationHandler = { [weak self] process in
            Task { @MainActor in
                guard let self else { return }
                stdout.fileHandleForReading.readabilityHandler = nil
                stderr.fileHandleForReading.readabilityHandler = nil
                self.process = nil
                self.input = nil
                if process.terminationStatus != 0 && self.state == .running {
                    self.fail("Setup stopped before it finished. Your existing model and completed downloads were kept.")
                }
            }
        }

        do {
            try process.run()
        } catch {
            fail("Could not start the installed Geer command: \(error.localizedDescription)")
        }
    }

    private func consume(_ data: Data) {
        stdoutBuffer.append(data)
        while let newline = stdoutBuffer.firstIndex(of: 0x0A) {
            let line = stdoutBuffer.prefix(upTo: newline)
            stdoutBuffer.removeSubrange(...newline)
            guard let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else {
                continue
            }
            Task { @MainActor in self.handle(object) }
        }
    }

    private func handle(_ event: [String: Any]) {
        guard (event["protocol_version"] as? Int) == 1 else {
            fail("This setup application and the installed Geer command use incompatible protocols.")
            return
        }
        switch event["event"] as? String {
        case "plan":
            if let plan = event["plan"] as? [String: Any] {
                modelName = plan["display_name"] as? String ?? modelName
                download = plan["download_human"] as? String ?? download
                requiredSpace = plan["required_free_human"] as? String ?? requiredSpace
                contextWindow = plan["context_window_human"] as? String ?? contextWindow
                kvCache = plan["kv_cache"] as? String ?? kvCache
            }
            retainedModel = event["retained_model"] as? Bool ?? false
        case "phase":
            phaseTitle = event["title"] as? String ?? "Preparing Geer"
            completed = Double(event["completed"] as? Int ?? 0)
            total = Double(event["total"] as? Int ?? 5)
        case "log":
            if let message = event["message"] as? String { appendLog(message) }
        case "decision_required":
            pendingDecisionID = event["decision_id"] as? String
            defaultDecision = event["default"] as? String ?? "yes"
            decisionPrompt = event["prompt"] as? String
        case "completed":
            let result = event["result"] as? [String: Any]
            if result?["status"] as? String == "cancelled" {
                state = .cancelled
                headline = "No changes were made"
                phaseTitle = "Setup cancelled"
                detail = "You can begin again here or run geer setup from a terminal. Existing model data was kept."
            } else {
                state = .ready
                headline = "Geer is ready"
                phaseTitle = "Setup completed"
                completed = total
                detail = "The local engine and repository retrieval passed their checks. You can close this window."
            }
        case "failed":
            fail(event["message"] as? String ?? "Setup could not be completed.")
        default:
            break
        }
    }

    func answer(_ choice: String) {
        guard let decisionID = pendingDecisionID, let input else { return }
        let response: [String: String] = ["decision_id": decisionID, "choice": choice]
        guard var data = try? JSONSerialization.data(withJSONObject: response) else { return }
        data.append(0x0A)
        do {
            try input.write(contentsOf: data)
            decisionPrompt = nil
            pendingDecisionID = nil
        } catch {
            fail("Could not send your choice to Geer: \(error.localizedDescription)")
        }
    }

    func cancel() {
        guard let process else {
            NSApplication.shared.terminate(nil)
            return
        }
        state = .cancelled
        headline = "Setup cancelled"
        detail = "Downloaded and verified files were kept. Open Geer Setup or run geer setup to continue later."
        phaseTitle = "Safe to resume"
        process.terminate()
        self.process = nil
    }

    func openTerminalFallback() {
        NSWorkspace.shared.open(URL(fileURLWithPath: fallbackCommand))
    }

    func copyLogs() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(logLines.joined(separator: "\n"), forType: .string)
    }

    private func appendLog(_ text: String) {
        logLines.append(contentsOf: text.split(whereSeparator: \.isNewline).map(String.init))
        if logLines.count > 500 { logLines.removeFirst(logLines.count - 500) }
    }

    private func fail(_ message: String) {
        state = .failed
        headline = "Geer needs your help"
        detail = message
        phaseTitle = "Setup did not finish"
    }
}

struct SetupView: View {
    @StateObject private var model = SetupViewModel()
    @State private var showLogs = false

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            HStack(spacing: 14) {
                GeerLogo()
                    .frame(width: 48, height: 48)
                VStack(alignment: .leading, spacing: 3) {
                    Text("GEER").font(.caption.weight(.bold)).tracking(2)
                    Text(model.headline).font(.title2.weight(.semibold))
                }
            }

            Text(model.detail).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)

            if model.state == .welcome {
                GroupBox {
                    VStack(alignment: .leading, spacing: 10) {
                        SetupFeatureRow(text: model.modelName, systemImage: "cpu")
                        SetupFeatureRow(
                            text: "Model size and context are calculated for this Mac",
                            systemImage: "internaldrive"
                        )
                        SetupFeatureRow(
                            text: "Geer asks before downloading",
                            systemImage: "checkmark.shield"
                        )
                        SetupFeatureRow(
                            text: "Existing ~/.geer model data is reused",
                            systemImage: "arrow.triangle.2.circlepath"
                        )
                        SetupFeatureRow(
                            text: "Claude Code and T3 Code are checked, not bundled",
                            systemImage: "app.badge.checkmark"
                        )
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
            } else if model.state == .running {
                VStack(alignment: .leading, spacing: 10) {
                    Text(model.phaseTitle).font(.headline)
                    ProgressView(value: model.completed, total: max(model.total, 1))
                    HStack {
                        Text(model.retainedModel ? "Matching retained model found — Geer will verify and reuse it" : "\(model.modelName) · \(model.download)")
                        Spacer()
                        Text("Required: \(model.requiredSpace)")
                    }.font(.caption).foregroundStyle(.secondary)
                    Text("Context: \(model.contextWindow) · KV cache: \(model.kvCache)")
                        .font(.caption).foregroundStyle(.secondary)
                }
            } else {
                Label(model.phaseTitle, systemImage: model.state == .ready ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                    .font(.headline)
                    .foregroundStyle(model.state == .ready ? .green : .orange)
            }

            if let prompt = model.decisionPrompt {
                GroupBox {
                    VStack(alignment: .leading, spacing: 12) {
                        Text(prompt).font(.headline)
                        Text("No files are downloaded or settings changed until you approve the relevant step.")
                            .font(.caption).foregroundStyle(.secondary)
                        HStack {
                            Button("No") { model.answer("no") }
                            Button("Continue") { model.answer("yes") }
                                .keyboardShortcut(.defaultAction)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
            }

            if !model.logLines.isEmpty {
                SetupDetailsView(lines: model.logLines, isExpanded: $showLogs)
            } else {
                Spacer(minLength: 0)
            }

            Divider()
            HStack {
                if model.state != .running && model.state != .ready {
                    Button("Use Terminal Instead") { model.openTerminalFallback() }
                }
                if !model.logLines.isEmpty {
                    Button("Copy Logs") { model.copyLogs() }
                }
                Spacer()
                if model.state == .welcome {
                    Button("Begin Setup") { model.begin() }.keyboardShortcut(.defaultAction)
                } else if model.state == .running {
                    Button("Cancel") { model.cancel() }.keyboardShortcut(.cancelAction)
                } else if model.state == .failed || model.state == .cancelled {
                    Button("Try Again") { model.begin() }.keyboardShortcut(.defaultAction)
                } else {
                    Button("Close") { NSApplication.shared.terminate(nil) }.keyboardShortcut(.defaultAction)
                }
            }
        }
        .padding(28)
        .frame(width: 680, height: 580)
    }
}

@main
struct GeerSetupApp: App {
    var body: some Scene {
        WindowGroup("Geer Setup") { SetupView() }
            .windowResizability(.contentSize)
    }
}
