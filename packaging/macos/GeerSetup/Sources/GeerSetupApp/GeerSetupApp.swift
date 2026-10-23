import AppKit
import CoreFoundation
import SwiftUI

private let setupCommand = "/usr/local/bin/geer"
private let fallbackCommand = "/Library/Application Support/Geer/app/geer-setup.command"
private let protocolVersion = 2

struct SetupModelOption: Identifiable {
    let id: String
    let name: String
    let eligible: Bool
    let reason: String
    let variant: String
    let context: String
    let download: String
    let requiredSpace: String
    let footprint: String
    let labels: [String]

    init?(_ row: [String: Any]) {
        guard let id = row["family"] as? String,
              let name = row["display_name"] as? String,
              let eligible = row["eligible"] as? Bool,
              let context = row["context_window_human"] as? String,
              let download = row["download_human"] as? String,
              let requiredSpace = row["required_free_human"] as? String,
              let footprint = row["installed_human"] as? String else { return nil }
        let variant = row["model_variant"] as? String
        guard variant != nil || !eligible else { return nil }
        self.id = id
        self.name = name
        self.eligible = eligible
        self.reason = row["reason"] as? String ?? ""
        self.variant = variant ?? "Unavailable"
        self.context = context
        self.download = download
        self.requiredSpace = requiredSpace
        self.footprint = footprint
        self.labels = [("recommended", "Recommended"), ("active", "Current"),
                       ("installed", "Downloaded and verified")].compactMap { key, label in
            row[key] as? Bool == true ? label : nil
        }
    }
}

struct SetupBenchmarkRow: Identifiable {
    let id: String
    let label: String
    let description: String
    let ornith: String
    let sage: String
}

struct SetupBenchmarkComparison {
    let title: String
    let note: String
    let methodology: String
    let sourceURL: URL
    let rows: [SetupBenchmarkRow]

    init?(_ value: [String: Any]) {
        guard let title = value["title"] as? String,
              let note = value["note"] as? String,
              let methodology = value["methodology"] as? String,
              let source = value["source_url"] as? String,
              let sourceURL = URL(string: source), sourceURL.scheme == "https",
              let rows = value["rows"] as? [[String: Any]] else { return nil }
        var parsed: [SetupBenchmarkRow] = []
        func score(_ value: Any?) -> String? {
            guard let number = value as? NSNumber,
                  CFGetTypeID(number) != CFBooleanGetTypeID(),
                  number.doubleValue.isFinite else { return nil }
            return String(format: "%.1f", locale: Locale(identifier: "en_US_POSIX"), number.doubleValue)
        }
        for row in rows {
            guard let id = row["id"] as? String, let label = row["label"] as? String,
                  let description = row["description"] as? String,
                  let ornith = score(row["ornith"]), let sage = score(row["sage"]) else { return nil }
            parsed.append(SetupBenchmarkRow(id: id, label: label, description: description,
                                           ornith: ornith, sage: sage))
        }
        self.title = title
        self.note = note
        self.methodology = methodology
        self.sourceURL = sourceURL
        self.rows = parsed
    }
}

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
    @Published var detail = "Geer prepares its local engine and repository search, reuses verified model data, and installs Pi and T3 Code automatically."
    @Published var phaseTitle = "Ready to begin"
    @Published var completed = 0.0
    @Published var total = 5.0
    @Published var logLines: [String] = []
    @Published var decisionPrompt: String?
    @Published var defaultDecision = "yes"
    @Published var retainedModel = false
    @Published var modelName = "Choose Sage or Ornith for this Mac"
    @Published var download = "Calculated before download"
    @Published var contextWindow = "Calculated for this Mac"
    @Published var kvCache = "BF16"
    @Published var requiredSpace = "Calculated before download"
    @Published var modelOptions: [SetupModelOption] = []
    @Published var selectedFamily = ""
    @Published var choosingModel = false
    @Published var comparison: SetupBenchmarkComparison?
    @Published var modelVariant = ""
    @Published var modelFootprint = "Calculated before download"

    var selectedModel: SetupModelOption? {
        modelOptions.first { $0.id == selectedFamily }
    }

    private var process: Process?
    private var input: FileHandle?
    private var stdoutBuffer = Data()
    private var pendingDecisionID: String?
    private var exitStatus: Int32?
    private var stdoutFinished = false
    private var t3ApplicationURL: URL?

    func begin() {
        guard process == nil else { return }
        stdoutBuffer.removeAll(keepingCapacity: true)
        exitStatus = nil
        stdoutFinished = false
        decisionPrompt = nil
        pendingDecisionID = nil
        choosingModel = false
        modelOptions = []
        comparison = nil
        t3ApplicationURL = nil
        state = .running
        headline = "Preparing Geer"
        detail = "You can safely cancel and relaunch setup. Verified model files are retained."

        let process = Process()
        let stdout = Pipe()
        let stderr = Pipe()
        let stdin = Pipe()
        process.executableURL = URL(fileURLWithPath: setupCommand)
        process.arguments = ["setup", "--frontend", "json-v2"]
        process.standardOutput = stdout
        process.standardError = stderr
        process.standardInput = stdin
        self.process = process
        self.input = stdin.fileHandleForWriting

        stderr.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard let text = String(data: data, encoding: .utf8), !text.isEmpty else { return }
            Task { @MainActor [weak self] in
                guard let self, self.process === process else { return }
                self.appendLog(text)
            }
        }
        process.terminationHandler = { [weak self] process in
            stderr.fileHandleForReading.readabilityHandler = nil
            DispatchQueue.main.async { [weak self] in
                guard let self, self.process === process else { return }
                self.exitStatus = process.terminationStatus
                self.finishIfExited()
            }
        }

        do {
            try process.run()
            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                while true {
                    let data = stdout.fileHandleForReading.availableData
                    if data.isEmpty { break }
                    DispatchQueue.main.async { [weak self] in
                        guard let self, self.process === process else { return }
                        self.consume(data)
                    }
                }
                DispatchQueue.main.async { [weak self] in
                    guard let self, self.process === process else { return }
                    self.stdoutFinished = true
                    self.finishIfExited()
                }
            }
        } catch {
            stderr.fileHandleForReading.readabilityHandler = nil
            self.process = nil
            self.input = nil
            fail("Could not start the installed Geer command: \(error.localizedDescription)")
        }
    }

    private func consume(_ data: Data) {
        guard state != .failed && state != .cancelled else { return }
        stdoutBuffer.append(data)
        while let newline = stdoutBuffer.firstIndex(of: 0x0A) {
            let line = stdoutBuffer.prefix(upTo: newline)
            stdoutBuffer.removeSubrange(...newline)
            guard let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else {
                fail("The installed Geer command returned invalid setup data. Run geer setup in a terminal to inspect the failure.")
                return
            }
            handle(object)
        }
    }

    private func finishIfExited() {
        guard stdoutFinished, let exitStatus else { return }
        self.process = nil
        self.input = nil
        if !stdoutBuffer.isEmpty && state != .failed && state != .cancelled {
            fail("The setup command ended with incomplete setup data. Run geer setup to finish.")
        } else if exitStatus == 2 && state != .failed && state != .cancelled {
            fail("This setup application and the installed Geer command use incompatible setup options. Reinstall matching versions or run geer setup in a terminal.")
        } else if exitStatus != 0 && state != .failed && state != .cancelled {
            fail("The setup command exited with status \(exitStatus). Your existing model and completed downloads were kept. Run geer setup to retry.")
        } else if state == .running {
            fail("Setup ended without confirming that Geer and T3 Code are ready. Run geer setup to finish.")
        }
    }

    private func handle(_ event: [String: Any]) {
        guard state != .failed && state != .cancelled else { return }
        guard (event["protocol_version"] as? Int) == protocolVersion else {
            fail("This setup application and the installed Geer command use incompatible protocols.")
            return
        }
        switch event["event"] as? String {
        case "model_choice_required":
            guard readCatalog(event), let decisionID = event["decision_id"] as? String else {
                fail("Geer returned an invalid model chooser. Reinstall matching versions of Geer and Geer Setup.")
                return
            }
            pendingDecisionID = decisionID
            choosingModel = true
            phaseTitle = "Choose a local model"
        case "plan":
            guard readCatalog(event) else {
                fail("Geer returned an invalid model plan. Reinstall matching versions of Geer and Geer Setup.")
                return
            }
            if let plan = event["plan"] as? [String: Any] {
                modelName = plan["display_name"] as? String ?? modelName
                download = plan["download_human"] as? String ?? download
                requiredSpace = plan["required_free_human"] as? String ?? requiredSpace
                contextWindow = plan["context_window_human"] as? String ?? contextWindow
                kvCache = plan["kv_cache"] as? String ?? kvCache
                modelVariant = plan["model_variant"] as? String ?? modelVariant
                modelFootprint = plan["installed_human"] as? String ?? modelFootprint
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
            guard let result = event["result"] as? [String: Any],
                  let status = result["status"] as? String else {
                fail("The setup command returned an invalid completion result. Run geer setup to finish.")
                return
            }
            if status == "cancelled" {
                state = .cancelled
                headline = "Setup cancelled"
                phaseTitle = "Setup cancelled"
                detail = "You can begin again here or run geer setup from a terminal. Existing model data was kept."
            } else if status == "ready" {
                guard let configured = result["t3_configured"] as? NSNumber,
                      CFGetTypeID(configured) == CFBooleanGetTypeID(),
                      configured.boolValue else {
                    fail("Setup did not confirm that T3 Code is configured. Run geer setup to finish.")
                    return
                }
                guard let path = result["t3_application_path"] as? String,
                      path.hasPrefix("/"), !path.contains("\0") else {
                    fail("Setup did not return an absolute T3 Code application path. Run geer setup to finish.")
                    return
                }
                t3ApplicationURL = URL(fileURLWithPath: path)
                state = .ready
                headline = "Geer is ready"
                phaseTitle = "Setup completed"
                completed = total
                detail = "Open a project in T3 Code, select the Geer model, and use Full access. On first launch, choose Continue to proceed without cloud sign-in."
            } else {
                fail("The setup command returned an unknown completion status: \(status). Run geer setup to finish.")
            }
        case "failed":
            fail(event["message"] as? String ?? "Setup could not be completed.")
        default:
            break
        }
    }

    private func readCatalog(_ event: [String: Any]) -> Bool {
        guard let catalog = event["catalog"] as? [String: Any],
              let selected = catalog["selected"] as? String,
              let rows = catalog["models"] as? [[String: Any]],
              let rawComparison = event["comparison"] as? [String: Any],
              let comparison = SetupBenchmarkComparison(rawComparison) else { return false }
        let models = rows.compactMap(SetupModelOption.init)
        guard models.count == 2, models.count == rows.count,
              Set(models.map(\.id)) == Set(["ornith", "sage"]),
              models.contains(where: { $0.id == selected }) else { return false }
        modelOptions = models
        selectedFamily = selected
        self.comparison = comparison
        return true
    }

    func answer(_ choice: String) {
        guard let decisionID = pendingDecisionID, let input else { return }
        if choosingModel && choice != "cancel" {
            guard modelOptions.contains(where: { $0.id == choice && $0.eligible }) else { return }
        }
        let response: [String: Any] = ["protocol_version": protocolVersion,
                                       "decision_id": decisionID, "choice": choice]
        guard var data = try? JSONSerialization.data(withJSONObject: response) else { return }
        data.append(0x0A)
        do {
            try input.write(contentsOf: data)
            decisionPrompt = nil
            pendingDecisionID = nil
            choosingModel = false
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
        self.input = nil
        decisionPrompt = nil
        pendingDecisionID = nil
        choosingModel = false
    }

    func openTerminalFallback() {
        NSWorkspace.shared.open(URL(fileURLWithPath: fallbackCommand))
    }

    func openT3Code() {
        guard state == .ready, let t3ApplicationURL else { return }
        if !NSWorkspace.shared.open(t3ApplicationURL) {
            fail("Could not open T3 Code at \(t3ApplicationURL.path). Open the application manually or run geer setup to verify it.")
        }
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
        headline = "Geer setup failed"
        detail = message
        phaseTitle = "Setup did not finish"
        decisionPrompt = nil
        pendingDecisionID = nil
        choosingModel = false
        t3ApplicationURL = nil
        if process?.isRunning == true { process?.terminate() }
    }
}

struct SetupView: View {
    @StateObject private var model = SetupViewModel()
    @State private var showLogs = false
    @State private var showMethodology = false

    private var modelChooser: some View {
        GroupBox("Choose a local model") {
            VStack(alignment: .leading, spacing: 10) {
                Picker("Model family", selection: $model.selectedFamily) {
                    ForEach(model.modelOptions) { option in
                        Text(option.name + (option.labels.isEmpty ? "" : " · " + option.labels.joined(separator: " · ")))
                            .tag(option.id)
                            .disabled(!option.eligible)
                    }
                }
                .pickerStyle(.radioGroup)
                ForEach(model.modelOptions.filter { !$0.eligible }) { option in
                    Text("\(option.name): \(option.reason)")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if let selected = model.selectedModel {
                    Text("\(selected.variant) · Configured context: \(selected.context) · Images supported")
                    Text("Missing download estimate: \(selected.download)")
                    Text("Additional disk required: \(selected.requiredSpace) · Full model footprint: \(selected.footprint)")
                }
                Text("Context limits depend on available memory.")
                    .font(.caption).foregroundStyle(.secondary)
                if let comparison = model.comparison {
                    Divider()
                    Text(comparison.title).font(.headline)
                    Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 4) {
                        GridRow {
                            Text("Upstream result")
                            Text("Ornith")
                            Text("Sage")
                        }.fontWeight(.semibold)
                        ForEach(comparison.rows) { row in
                            GridRow {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(row.label)
                                    Text(row.description).font(.caption).foregroundStyle(.secondary)
                                }
                                Text(row.ornith)
                                Text(row.sage)
                            }
                        }
                    }
                    Text(comparison.note).font(.caption).foregroundStyle(.secondary)
                    DisclosureGroup("Methodology and source", isExpanded: $showMethodology) {
                        VStack(alignment: .leading, spacing: 6) {
                            Text(comparison.methodology)
                            ForEach(comparison.rows) { row in
                                Text("\(row.label): \(row.description)")
                            }
                            Link("Read the upstream comparison", destination: comparison.sourceURL)
                        }.font(.caption).textSelection(.enabled)
                    }
                }
                Button("Review Installation") { model.answer(model.selectedFamily) }
                    .disabled(model.selectedModel?.eligible != true)
                    .keyboardShortcut(.defaultAction)
            }
            .font(.callout)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

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
                            text: "Pi and T3 Code are installed and verified automatically; existing installations are reused",
                            systemImage: "app.badge.checkmark"
                        )
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
            } else if model.state == .running && model.choosingModel {
                ScrollView { modelChooser }
            } else if model.state == .running {
                VStack(alignment: .leading, spacing: 10) {
                    Text(model.phaseTitle).font(.headline)
                    ProgressView(value: model.completed, total: max(model.total, 1))
                    HStack {
                        Text(model.retainedModel ? "Current verified model will be reused" : model.modelName)
                        Spacer()
                        Text("Additional disk: \(model.requiredSpace)")
                    }.font(.caption).foregroundStyle(.secondary)
                    Text("\(model.modelVariant) · Configured context: \(model.contextWindow) · KV cache: \(model.kvCache)")
                        .font(.caption).foregroundStyle(.secondary)
                    Text("Missing download estimate: \(model.download) · Full model footprint: \(model.modelFootprint)")
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

            if !model.logLines.isEmpty && !model.choosingModel {
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
                    Button("Close") { NSApplication.shared.terminate(nil) }.keyboardShortcut(.cancelAction)
                    Button("Open T3 Code") { model.openT3Code() }.keyboardShortcut(.defaultAction)
                }
            }
        }
        .padding(28)
        .frame(width: 780, height: 760)
    }
}

@main
struct GeerSetupApp: App {
    var body: some Scene {
        WindowGroup("Geer Setup") { SetupView() }
            .windowResizability(.contentSize)
    }
}
