import AppKit
import Foundation
import CoreText
import CoreFoundation

private enum AppBrand {
    static let displayName = Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? "模型灯塔"
    static let productName = Bundle.main.object(forInfoDictionaryKey: "CFBundleName") as? String ?? "Model Beacon"
    static let icon = Bundle.main.url(forResource: "AppIcon", withExtension: "icns").flatMap { NSImage(contentsOf: $0) }
}

enum MonitorTheme: String, CaseIterable {
    case graphite, silver

    var title: String { self == .graphite ? "石墨冰蓝" : "银白冷蓝" }
}

private func rgb(_ red: CGFloat, _ green: CGFloat, _ blue: CGFloat, _ alpha: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: red, green: green, blue: blue, alpha: alpha)
}

private struct MonitorPalette {
    let dark: Bool
    var ink: NSColor { dark ? rgb(0.88, 0.94, 1) : rgb(0.05, 0.09, 0.20) }
    var muted: NSColor { dark ? rgb(0.65, 0.72, 0.81) : rgb(0.36, 0.42, 0.54) }
    var quiet: NSColor { dark ? rgb(0.54, 0.63, 0.74) : rgb(0.47, 0.57, 0.72) }
    var top: NSColor { dark ? rgb(33 / 255, 42 / 255, 54 / 255) : rgb(248 / 255, 250 / 255, 254 / 255) }
    var bottom: NSColor { dark ? rgb(26 / 255, 34 / 255, 44 / 255) : rgb(246 / 255, 249 / 255, 254 / 255) }
    var rim: NSColor { dark ? rgb(0.37, 0.46, 0.58) : rgb(0.67, 0.76, 0.92) }
    var innerRim: NSColor { dark ? rgb(0.055, 0.075, 0.10) : rgb(1, 1, 1, 0.85) }
    var line: NSColor { dark ? rgb(0.34, 0.42, 0.51, 0.65) : rgb(0.76, 0.82, 0.92, 0.70) }
    var blue: NSColor { dark ? rgb(0.48, 0.78, 1) : rgb(0.17, 0.40, 0.85) }
    var blueBack: NSColor { dark ? rgb(35 / 255, 52 / 255, 72 / 255) : rgb(230 / 255, 240 / 255, 254 / 255) }
    var blueRim: NSColor { dark ? rgb(44 / 255, 71 / 255, 102 / 255) : rgb(0.79, 0.87, 0.98) }
    var amber: NSColor { dark ? rgb(1, 0.75, 0.39) : rgb(0.67, 0.36, 0.08) }
    var amberRim: NSColor { dark ? rgb(0.78, 0.57, 0.28) : rgb(0.93, 0.75, 0.43) }
    var amberBack: NSColor { dark ? rgb(45 / 255, 41 / 255, 37 / 255) : rgb(252 / 255, 246 / 255, 236 / 255) }
    var green: NSColor { dark ? rgb(0.44, 0.85, 0.70) : rgb(0.10, 0.53, 0.37) }
    var button: NSColor { dark ? rgb(0.16, 0.20, 0.25) : rgb(0.93, 0.96, 1) }
}

private func timestampString(_ value: Any?) -> String? {
    if let value = value as? String { return value }
    if let value = value as? NSNumber { return ISO8601DateFormatter().string(from: Date(timeIntervalSince1970: value.doubleValue)) }
    return nil
}

private func readableTime(_ value: String?) -> String {
    guard let value else { return "未记录" }
    let parser = ISO8601DateFormatter()
    parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    guard let date = parser.date(from: value) ?? ISO8601DateFormatter().date(from: value) else { return value }
    let display = DateFormatter()
    display.dateFormat = "MM-dd HH:mm:ss"
    return display.string(from: date)
}

private func durationValue(_ value: Any?) -> Double? {
    guard let value = value as? NSNumber else { return nil }
    let seconds = value.doubleValue
    return seconds.isFinite && seconds >= 0 && seconds <= 86_400 ? seconds : nil
}

private func checkpointCount(_ value: Any?) -> Int? {
    guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
    let count = number.doubleValue
    guard count.isFinite, count >= 0, count < Double(Int.max), count.rounded(.towardZero) == count else { return nil }
    return number.intValue
}

private func durationLabel(_ seconds: Double?) -> String {
    guard let seconds else { return "未记录" }
    return String(format: "%.0fs", seconds)
}

struct SampleTiming {
    let attempt: Int
    let totalSeconds: Double
    let startupSeconds: Double?
    let metadataSeconds: Double?
    let forkSeconds: Double?
    let modelSeconds: Double?
    init?(json: [String: Any], index: Int) {
        guard let total = durationValue(json["total_seconds"]) else { return nil }
        attempt = min(4, max(1, json["attempt"] as? Int ?? index + 1))
        totalSeconds = total
        startupSeconds = durationValue(json["startup_seconds"])
        metadataSeconds = durationValue(json["metadata_seconds"])
        forkSeconds = durationValue(json["fork_seconds"])
        modelSeconds = durationValue(json["model_seconds"])
    }
    var label: String { "第\(attempt)份 \(durationLabel(totalSeconds))" }
    var detail: String {
        "启动 \(durationLabel(startupSeconds)) · 元数据 \(durationLabel(metadataSeconds)) · 分支 \(durationLabel(forkSeconds)) · 模型 \(durationLabel(modelSeconds))"
    }
}

struct SessionOption {
    let id: String
    let title: String
    init?(json: [String: Any]) {
        guard let id = json["session_id"] as? String else { return nil }
        self.id = id
        title = json["session_title"] as? String ?? "未命名会话"
    }
}

struct AttributionSnapshot {
    var enabled = false
    var available = false
    var manualAvailable: Bool?
    var automaticAvailable: Bool?
    var requiresNewSample = false
    var unavailableReason = "正在检查目标会话、采样入口和数字指纹参考。"
    var persistenceError: String?
    var referenceStatus: String?
    var referenceWarning: String?
    var samplerStatus: String?
    var status = "idle"
    var progress = "尚未进行数字归因"
    var previewLabel: String?
    var currentCandidate: String?
    var elapsedSeconds: Double?
    var phase: String?
    var taskID: String?
    var taskSessionID: String?
    var taskSource: String?
    var autoTools: Int?
    var autoThreshold: Int?
    var autoPending = false
    var autoLastCompletedAt: String?
    var cancellable = false
    var result: [String: Any]?
    var scope = "history"
    var sampleScope: String?
    var completedAt: String?
    var source: String?
    var validSamples = 0
    var attempts = 0
    var reasonShort: String?
    var model: String?
    var provider: String?
    var referenceVersion: String?
    var restored = false
    var sampleTimings: [SampleTiming] = []
    var sampleCandidates: [String] = []
    var stability: String?
    var confirmation: [String: Any]?
    var differenceHistory: [[String: Any]] = []

    init() {}
    init(json: [String: Any]) {
        enabled = json["enabled"] as? Bool ?? false
        available = json["available"] as? Bool ?? false
        manualAvailable = json["manual_available"] as? Bool
        automaticAvailable = json["auto_available"] as? Bool
        requiresNewSample = json["requires_new_sample"] as? Bool ?? false
        unavailableReason = json["unavailable_reason"] as? String ?? "当前会话暂不支持数字归因。"
        persistenceError = json["persistence_error"] as? String
        referenceStatus = json["reference_status"] as? String
        referenceWarning = json["reference_warning"] as? String
        samplerStatus = json["sampler_status"] as? String
        status = json["status"] as? String ?? "idle"
        progress = json["progress"] as? String ?? "尚未进行数字归因"
        if let task = json["task"] as? [String: Any] {
            taskID = task["id"] as? String
            taskSessionID = task["session_id"] as? String
            cancellable = task["cancellable"] as? Bool ?? false
            status = task["status"] as? String ?? status
            progress = task["progress"] as? String ?? progress
        }
        result = json["result"] as? [String: Any]
        let task = json["task"] as? [String: Any]
        taskSource = task?["source"] as? String
        if let automatic = json["auto_progress"] as? [String: Any] {
            autoTools = checkpointCount(automatic["tools"])
            if let threshold = checkpointCount(automatic["threshold"]), (16...32).contains(threshold) { autoThreshold = threshold }
            autoPending = automatic["pending"] as? Bool ?? false
            autoLastCompletedAt = timestampString(automatic["last_completed_at"])
        }
        if task != nil || ["preparing", "queued", "running", "cancelling"].contains(status) {
            confirmation = json["confirmation"] as? [String: Any] ?? task?["confirmation"] as? [String: Any]
        } else {
            confirmation = json["confirmation"] as? [String: Any] ?? result?["confirmation"] as? [String: Any]
        }
        differenceHistory = Array(((json["difference_history"] as? [[String: Any]]) ?? []).suffix(5))
        currentCandidate = (json["current_candidate"] as? String ?? task?["current_candidate"] as? String).map { String($0.prefix(128)) }
        previewLabel = (json["preview_label"] as? String ?? task?["preview_label"] as? String).map { String($0.prefix(100)) }
        elapsedSeconds = durationValue(json["elapsed_seconds"] ?? task?["elapsed_seconds"])
        phase = json["phase"] as? String ?? task?["phase"] as? String
        sampleTimings = ((result?["sample_timings"] as? [[String: Any]]) ?? []).prefix(4).enumerated().compactMap { SampleTiming(json: $0.element, index: $0.offset) }
        sampleCandidates = Array(((result?["sample_candidates"] as? [String]) ?? []).prefix(4))
        stability = result?["stability"] as? String
        scope = json["scope"] as? String ?? "history"
        sampleScope = json["sample_scope"] as? String ?? result?["sample_scope"] as? String ?? (json["task"] as? [String: Any])?["sample_scope"] as? String
        completedAt = timestampString(json["completed_at"]) ?? timestampString(result?["completed_at"])
        source = json["source"] as? String ?? result?["source"] as? String
        validSamples = json["valid_samples"] as? Int ?? result?["valid_samples"] as? Int ?? 0
        attempts = json["attempts"] as? Int ?? result?["attempts"] as? Int ?? 0
        reasonShort = (json["reason_short"] as? String ?? result?["reason_short"] as? String).map { String($0.prefix(80)) }
        model = json["model"] as? String ?? result?["model"] as? String
        provider = json["provider"] as? String ?? result?["provider"] as? String
        referenceVersion = json["reference_version"] as? String ?? result?["reference_version"] as? String
        restored = json["restored"] as? Bool ?? false
        if status == "preparing" && progress == "尚未进行数字归因" { progress = "正在核对会话配置" }
    }
    var busy: Bool { ["preparing", "queued", "running", "cancelling"].contains(status) }
    var cancelled: Bool { result?["cancelled"] as? Bool == true }
    var confirmationStatus: String? { confirmation?["status"] as? String }
    var hasInitialDifference: Bool { confirmation?["initial_mismatch"] as? Bool == true }
    var warning: Bool {
        if busy { return hasInitialDifference }
        return !cancelled && (hasInitialDifference || ["mismatch", "different", "inconsistent"].contains(result?["outcome"] as? String ?? ""))
    }
    var candidate: String? { result?["candidate"] as? String ?? (result?["candidates"] as? [String])?.first }
    var attributedModel: String? {
        if let candidate { return candidate }
        guard result?["outcome"] as? String == "close" else { return nil }
        return result?["model"] as? String ?? model
    }
    var candidateLabel: String? { attributedModel.map { "返回模型  " + $0 } }
    var progressSummary: String {
        if status == "cancelling" { return "正在取消归因" }
        guard let previewLabel, !previewLabel.isEmpty else { return progress }
        return progress + " · " + previewLabel
    }
    var stabilityLabel: String? {
        switch stability {
        case "mixed": return "稳定性：候选分歧"
        case "consistent", "close": return "稳定性：候选一致"
        case "different": return "稳定性：指纹有差异"
        case "low_margin", "boundary": return "稳定性：参考接近"
        default: return Set(sampleCandidates).count > 1 ? "稳定性：候选分歧" : nil
        }
    }
    var scopeLabel: String {
        if sampleScope == "new_session" { return restored || scope == "history" ? "新会话采样 · 历史记录" : "新会话采样 · 本次采样" }
        if restored || scope == "history" { return "历史归因记录" }
        if scope == "previous_config" { return "上一配置的归因" }
        if scope == "previous_turn" { return "上次归因，不代表本轮" }
        return "最近数字归因 · 本次采样"
    }
    var resultLabel: String {
        guard let result else { return "尚无数字归因结果" }
        if cancelled { return "已取消归因" }
        var label = result["label"] as? String ?? "未得出结论"
        if validSamples == 0 && (attempts > 0 || reasonShort != nil || ["准备采样失败", "采样未完成"].contains(label)) &&
            (["inconclusive", "failed", "error"].contains(result["outcome"] as? String ?? "") || ["准备采样失败", "采样未完成"].contains(label)) {
            return attempts == 0 ? "准备采样失败" : "采样未完成"
        }
        if result["outcome"] as? String == "close" || label.hasPrefix("接近") {
            return attributedModel.map { "归因模型 " + $0 } ?? "归因匹配"
        }
        if result["reference_supported"] as? Bool == false,
           result["outcome"] as? String == "inconclusive", candidate != nil, label == "未得出结论" {
            return "参考倾向 " + (candidate ?? "待复测")
        }
        if restored || scope != "current" {
            label = label.replacingOccurrences(of: "当前所选模型", with: "该次所选模型")
                .replacingOccurrences(of: "接近所选模型", with: "接近该次所选模型")
                .replacingOccurrences(of: "与所选模型不符", with: "与该次所选模型不符")
        }
        return label
    }
    var resultSummary: String {
        if isFailure, let reasonShort, !reasonShort.isEmpty { return resultLabel + " · " + reasonShort }
        return resultLabel
    }
    var isFailure: Bool { ["准备采样失败", "采样未完成"].contains(resultLabel) }
    var recordReason: String {
        if cancelled { return "用户取消" }
        if isFailure {
            if let reasonShort, reasonShort.range(of: #"[\p{Han}]"#, options: .regularExpression) != nil {
                return String(reasonShort.prefix(22))
            }
            return resultLabel
        }
        let confirmation = result?["confirmation"] as? [String: Any]
        switch confirmation?["status"] as? String {
        case "confirmed_mismatch": return "三次复测均不一致"
        case "not_confirmed": return "复测未持续异常"
        case "incomplete", "active": return "复测未完成"
        default: break
        }
        switch result?["outcome"] as? String {
        case "close": return "匹配所选型号"
        case "different": return "返回型号与配置不一致"
        default:
            if result?["reason_code"] as? String == "expected_unenrolled" { return "所选型号未收录" }
            return "归因结果待复测"
        }
    }

}

// The card presents numeric sampling references and the selected configuration.
struct ModelSnapshot {
    var status = "ready"
    var selectedModel: String?
    var selectedEffort: String?
    var selectedProvider: String?
    var configurationModel: String?
    var configurationEffort: String?
    var configurationProvider: String?
    var detail = ""
    var sessionID: String?
    var sessionTitle: String?
    var selectionMode = "recent_activity"
    var sourceFile: String?
    var turnID: String?
    var contextSegment: String?
    var sessions: [SessionOption] = []
    var attribution = AttributionSnapshot()
    var manualAvailable: Bool?
    var automaticAvailable: Bool?
    var requiresNewSample = false
    var commandError: String?
    var historyError: String?
    var switchingSession = false
    var automaticSettingKnown = false

    init() {}
    init(json: [String: Any]) {
        status = json["status"] as? String == "error" ? "error" : "ready"
        selectedModel = json["selected_model"] as? String
        selectedEffort = json["selected_effort"] as? String
        selectedProvider = json["selected_provider"] as? String
        configurationModel = json["configuration_model"] as? String
        configurationEffort = json["configuration_effort"] as? String
        configurationProvider = json["configuration_provider"] as? String
        detail = json["detail"] as? String ?? ""
        sessionID = json["session_id"] as? String ?? json["bound_session_id"] as? String
        sessionTitle = json["session_title"] as? String
        sourceFile = json["source_file"] as? String
        selectionMode = json["selection_mode"] as? String ?? "recent_activity"
        turnID = json["turn_id"] as? String
        contextSegment = (json["context_segment"] as? String) ?? (json["context_segment"] as? NSNumber)?.stringValue
        sessions = (json["sessions"] as? [[String: Any]] ?? []).compactMap(SessionOption.init)
        if let object = json["attribution"] as? [String: Any] {
            attribution = AttributionSnapshot(json: object)
            automaticSettingKnown = object["enabled"] is Bool
        }
        manualAvailable = json["manual_available"] as? Bool
        automaticAvailable = json["auto_available"] as? Bool
        requiresNewSample = json["requires_new_sample"] as? Bool ?? attribution.requiresNewSample
        if let scope = json["sample_scope"] as? String { attribution.sampleScope = scope }
        commandError = json["command_error"] as? String
        historyError = json["history_error"] as? String ?? (json["attribution"] as? [String: Any])?["persistence_error"] as? String
    }
    var samplingModel: String? { selectedModel ?? configurationModel }
    var configurationLabel: String { selectedModel != nil ? "所选型号" : "采样配置" }
    var referenceValue: String {
        if attribution.busy { return attribution.currentCandidate ?? "归因中" }
        if attribution.cancelled { return "已取消" }
        if attribution.isFailure { return attribution.reasonShort ?? attribution.resultLabel }
        if let model = attribution.attributedModel { return model }
        if attribution.result != nil { return "未返回" }
        return "尚未归因"
    }
    var attributionRowLabel: String { "返回模型" }
    var lastDetectionLine: String? {
        let independent = attribution.sampleScope == "new_session"
        guard let time = attribution.completedAt ?? (independent ? nil : attribution.autoLastCompletedAt) else { return nil }
        return (independent ? "独立检测 · " : "上次检测 · ") + readableTime(time)
    }
    var automaticStatus: String {
        let a = attribution
        if !a.enabled { return "自动监测已关闭 · " + bindingLabel }
        if status == "error" { return "自动监测已开启 · 日志读取失败" }
        if switchingSession { return "自动监测已开启 · 正在切换会话" }
        if a.busy && a.taskSource == "automatic" { return "自动监测执行中" }
        if sessionID == nil { return "自动监测已开启 · 等待会话" }
        if a.autoPending { return "自动监测 · 等待当前任务" }
        if let tools = a.autoTools, let threshold = a.autoThreshold { return "自动监测 · 工具完成 \(tools)/\(threshold)" }
        return "自动监测已开启"
    }
    var attributionFooter: String {
        let a = attribution
        if let commandError { return commandError }
        if status == "error" { return detail }
        if a.busy { return (a.sampleScope == "new_session" ? "新会话归因 · " : "") + a.progressSummary }
        if a.cancelled { return "已取消归因" }
        if a.result != nil {
            return lastDetectionLine ?? "检测时间未记录"
        }
        return "右键开始归因"
    }
    var numericStatus: String {
        if commandError != nil || status == "error" { return "error" }
        if attribution.status == "cancelling" { return "cancelling" }
        if attribution.busy { return attribution.hasInitialDifference ? "confirming" : "running" }
        if attribution.cancelled { return "cancelled" }
        guard let result = attribution.result else { return "idle" }
        if attribution.isFailure { return "error" }
        let outcome = result["outcome"] as? String ?? "inconclusive"
        return ["close", "different"].contains(outcome) ? outcome : "inconclusive"
    }
    var label: String {
        if commandError != nil { return "归因未启动" }
        if status == "error" { return "记录读取失败" }
        switch numericStatus {
        case "cancelling": return "正在取消归因"
        case "cancelled": return "已取消归因"
        case "confirming": return "发现差异 · 复测中"
        case "running": return attribution.status == "preparing" ? "正在准备采样" : "数字归因中"
        case "close": return "归因匹配"
        case "different": return attribution.confirmationStatus == "confirmed_mismatch" ? "持续指纹差异" : "指纹存在差异"
        case "inconclusive":
            if attribution.confirmationStatus == "not_confirmed" { return "复测未持续异常" }
            if attribution.confirmationStatus == "incomplete" { return "复测未完成" }
            return attribution.candidate == nil ? "参考待复测" : "已得出参考候选"
        case "error": return attribution.resultLabel
        default: return "尚未归因"
        }
    }
    var badge: String {
        guard let model = samplingModel else { return "MB" }
        if let range = model.range(of: #"\d+(?:\.\d+)?"#, options: .regularExpression) { return String(model[range]) }
        return "MB"
    }
    var bindingLabel: String { selectionMode == "bound" ? "已绑定" : "跟随最近活跃会话" }
    var canAttribute: Bool { (sessionID != nil || requiresNewSample) && (manualAvailable ?? attribution.manualAvailable ?? attribution.available) && !attribution.busy }
    var canEnableAutomatic: Bool { sessionID != nil && !switchingSession && (automaticAvailable ?? attribution.automaticAvailable ?? (status != "error")) }
    var canSetAutomatic: Bool { attribution.enabled || canEnableAutomatic }
    func switching(to session: String?) -> ModelSnapshot {
        var cleared = ModelSnapshot()
        cleared.sessionID = session
        cleared.selectionMode = session == nil ? "recent_activity" : "bound"
        cleared.sessionTitle = sessions.first(where: { $0.id == session })?.title
        cleared.sessions = sessions
        cleared.attribution.enabled = attribution.enabled
        cleared.automaticSettingKnown = automaticSettingKnown
        cleared.switchingSession = true
        cleared.detail = "正在切换监测会话，等待目标会话自己的记录。"
        return cleared
    }
    func retainingAutomaticSetting(from previous: ModelSnapshot) -> ModelSnapshot {
        var state = self
        if status == "error", !automaticSettingKnown {
            state.attribution.enabled = previous.attribution.enabled
            state.automaticSettingKnown = previous.automaticSettingKnown
        }
        return state
    }
    func automaticCommand(enabled: Bool) -> [String: Any]? {
        guard !enabled || canEnableAutomatic else { return nil }
        var command: [String: Any] = ["action": "set_auto", "enabled": enabled]
        if let sessionID { command["session_id"] = sessionID } else { command["session_id"] = NSNull() }
        return command
    }
    var attributionReason: String {
        if sessionID == nil && requiresNewSample { return "选择新会话采样的型号和提供方。" }
        if sessionID == nil { return "尚无可采样的会话；右键可选择新会话采样。" }
        if attribution.busy { return attribution.progressSummary }
        return attribution.unavailableReason
    }
    var cancelAttributionCommand: [String: Any] {
        var command: [String: Any] = ["action": "cancel_attribution"]
        if let sessionID { command["session_id"] = sessionID } else { command["session_id"] = NSNull() }
        if let taskID = attribution.taskID { command["task_id"] = taskID } else { command["task_id"] = NSNull() }
        return command
    }
    static func failure(_ message: String) -> ModelSnapshot {
        var state = ModelSnapshot()
        state.status = "error"
        state.detail = message
        return state
    }
}

struct AttributionFooterLayout {
    let summary: String?
    let detection: String?
    let summaryRect: NSRect
    let detectionRect: NSRect?
    let detectionAlignment: NSTextAlignment

    init(state: ModelSnapshot, width: CGFloat) {
        detection = state.lastDetectionLine
        summary = state.attributionFooter == detection ? nil : state.attributionFooter
        let available = max(0, width - 30)
        let detectionWidth = min(158, available)
        detectionAlignment = summary == nil ? .left : .right
        detectionRect = detection == nil ? nil : summary == nil ?
            NSRect(x: 15, y: 102, width: available, height: 22) :
            NSRect(x: width - 15 - detectionWidth, y: 102, width: detectionWidth, height: 22)
        summaryRect = NSRect(x: 15, y: 102, width: detection == nil ? available : max(0, available - detectionWidth - 8), height: 22)
    }
}

final class NewSessionSampleForm: NSView {
    private static let supportedEfforts = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
    static let models = ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4"]
    let modelField = NSComboBox(frame: NSRect(x: 75, y: 40, width: 255, height: 27))
    let providerField = NSTextField(frame: NSRect(x: 75, y: 0, width: 255, height: 27))
    private let inheritedEffort: String

    init(state: ModelSnapshot) {
        let inherited = state.selectedEffort ?? state.configurationEffort ?? "ultra"
        inheritedEffort = Self.supportedEfforts.contains(inherited) ? inherited : "ultra"
        super.init(frame: NSRect(x: 0, y: 0, width: 330, height: 70))
        modelField.addItems(withObjectValues: Self.models)
        modelField.isEditable = true
        modelField.completes = true
        modelField.stringValue = state.selectedModel ?? state.configurationModel ?? "gpt-6.1-sol"
        modelField.setAccessibilityLabel("模型")
        providerField.stringValue = state.selectedProvider ?? state.configurationProvider ?? "openai"
        providerField.setAccessibilityLabel("提供方")
        for (title, field) in [("模型", modelField as NSControl), ("提供方", providerField as NSControl)] {
            let label = NSTextField(labelWithString: title)
            label.frame = NSRect(x: 0, y: field.frame.minY + 4, width: 65, height: 20)
            addSubview(label); addSubview(field)
        }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    var command: [String: Any]? {
        let model = modelField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let provider = providerField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard [model, provider].allSatisfy({ $0.range(of: #"^[A-Za-z0-9._:/-]{1,128}$"#, options: .regularExpression) != nil }) else { return nil }
        return ["action": "sample_new", "model": model, "provider": provider, "effort": inheritedEffort]
    }
}

final class PassiveReader {
    private var process: Process?
    private let input = Pipe()
    private let output = Pipe()
    private let errors = Pipe()
    private let queue = DispatchQueue(label: "app.modelbeacon.passive-reader")
    private var pending = Data()
    private var errorText = ""
    private var stopping = false
    var onUpdate: ((ModelSnapshot) -> Void)?

    private func publish(_ state: ModelSnapshot) {
        DispatchQueue.main.async { [weak self] in self?.onUpdate?(state) }
    }

    func start(session: String?) {
        let environment = ProcessInfo.processInfo.environment
        let fileManager = FileManager.default
        let resources = Bundle.main.resourceURL
        let packagedReader = resources?.appendingPathComponent("reader/model-beacon-reader")
        let buildInfo = resources?.appendingPathComponent("build-info.json")
        let buildMode = buildInfo.flatMap { try? Data(contentsOf: $0) }
            .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }?["mode"] as? String
        let executableDirectory = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL.deletingLastPathComponent()
        let candidates = [
            environment["PASSIVE_MONITOR_SCRIPT"].map { URL(fileURLWithPath: $0) },
            Bundle.main.resourceURL?.appendingPathComponent("passive_monitor.py"),
            executableDirectory.appendingPathComponent("passive_monitor.py"),
            executableDirectory.deletingLastPathComponent().appendingPathComponent("passive_monitor.py"),
            URL(fileURLWithPath: fileManager.currentDirectoryPath).appendingPathComponent("passive_monitor.py")
        ].compactMap { $0 }
        let child = Process()
        var arguments = ["--watch", "--interval", "1", "--control-stdin"]
        if let packagedReader, fileManager.isExecutableFile(atPath: packagedReader.path) {
            child.executableURL = packagedReader
        } else if buildMode == "release" {
            publish(.failure("安装包缺少读取组件，请重新下载安装。"))
            return
        } else {
            guard let script = candidates.first(where: { fileManager.fileExists(atPath: $0.path) }) else {
                publish(.failure("找不到本地读取组件，请重新构建开发版。"))
                return
            }
            let python = environment["PASSIVE_MONITOR_PYTHON"] ?? "/usr/bin/python3"
            guard python.hasPrefix("/"), fileManager.isExecutableFile(atPath: python) else {
                publish(.failure("开发版需要 Python 3，可设置 PASSIVE_MONITOR_PYTHON 的绝对路径。"))
                return
            }
            child.executableURL = URL(fileURLWithPath: python)
            arguments = ["-u", script.path] + arguments
        }
        if let session { arguments += ["--session", session] }
        if let stateFile = environment["MODEL_BEACON_STATE_FILE"], stateFile.hasPrefix("/") {
            arguments += ["--state-file", stateFile]
        }
        if let sessionsDirectory = environment["MODEL_BEACON_SESSIONS_DIR"], sessionsDirectory.hasPrefix("/") {
            arguments += ["--sessions-dir", sessionsDirectory]
        }
        child.arguments = arguments
        var readerEnvironment = environment.merging(["PYTHONDONTWRITEBYTECODE": "1"]) { _, new in new }
        // A packaged reader is self contained; inherited developer Python paths
        // must not change which runtime or standard library it loads.
        if buildMode == "release" {
            readerEnvironment.removeValue(forKey: "PYTHONHOME")
            readerEnvironment.removeValue(forKey: "PYTHONPATH")
        }
        child.environment = readerEnvironment
        child.standardInput = input
        child.standardOutput = output
        child.standardError = errors
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let bytes = handle.availableData
            guard !bytes.isEmpty else { handle.readabilityHandler = nil; return }
            self?.queue.async { [weak self] in self?.receive(bytes) }
        }
        errors.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let bytes = handle.availableData
            guard !bytes.isEmpty else { handle.readabilityHandler = nil; return }
            self?.queue.async { [weak self] in
                guard let self else { return }
                let text = String(data: bytes, encoding: .utf8) ?? ""
                self.errorText = String((self.errorText + text).suffix(4096))
            }
        }
        child.terminationHandler = { [weak self] child in
            self?.queue.async { [weak self] in
                guard let self, !self.stopping else { return }
                // Do not surface arbitrary stderr, which could contain local paths.
                self.publish(.failure("本地读取程序已退出（\(child.terminationStatus)）。退出并重新打开应用可重试。"))
            }
        }
        process = child
        do { try child.run() }
        catch { publish(.failure("无法启动本地读取程序：\(error.localizedDescription)")) }
    }

    private func receive(_ bytes: Data) {
        pending.append(bytes)
        if pending.count > 262_144 {
            pending.removeAll(keepingCapacity: false)
            publish(.failure("读取程序返回的数据过大，已忽略。"))
            return
        }
        while let newline = pending.firstIndex(of: 0x0A) {
            let line = pending.prefix(upTo: newline)
            pending.removeSubrange(...newline)
            guard !line.isEmpty else { continue }
            guard let object = try? JSONSerialization.jsonObject(with: line),
                  let json = object as? [String: Any],
                  [1, 2].contains(json["schema_version"] as? Int ?? 0) else {
                publish(.failure("本地读取程序返回了不兼容的状态格式。"))
                continue
            }
            publish(ModelSnapshot(json: json))
        }
    }

    @discardableResult func send(_ action: [String: Any]) -> Bool {
        guard process?.isRunning == true, let bytes = try? JSONSerialization.data(withJSONObject: action) else {
            publish(.failure("本地读取组件未运行，请重新打开应用。"))
            return false
        }
        do { try input.fileHandleForWriting.write(contentsOf: bytes + Data([0x0A])); return true }
        catch { publish(.failure("无法向本地读取组件发送操作：\(error.localizedDescription)")); return false }
    }

    func stop() {
        queue.sync { stopping = true }
        output.fileHandleForReading.readabilityHandler = nil
        errors.fileHandleForReading.readabilityHandler = nil
        if let process, process.isRunning { process.terminate() }
    }
}

final class FloatingPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

final class MonitorBodyView: NSView {
    weak var owner: MonitorView?
    override var isFlipped: Bool { true }
    override func draw(_ dirtyRect: NSRect) { owner?.drawBody() }
    override func mouseDown(with event: NSEvent) { owner?.mouseDown(with: event) }
    override func mouseDragged(with event: NSEvent) { owner?.mouseDragged(with: event) }
    override func mouseUp(with event: NSEvent) { owner?.mouseUp(with: event) }
    override func rightMouseDown(with event: NSEvent) { owner?.rightMouseDown(with: event) }
}

final class MonitorView: NSView {
    var theme: MonitorTheme = .graphite { didSet { needsDisplay = true; refreshControls() } }
    var state = ModelSnapshot() { didSet { submitting = false; submissionRestore = nil; needsDisplay = true; updateAccessibility(); refreshControls() } }
    var collapsed = true { didSet { needsDisplay = true; updateAccessibility(); needsLayout = true; refreshControls() } }
    var lockedSession: String? { didSet { needsDisplay = true; updateAccessibility() } }
    var onToggle: (() -> Void)?
    var onKeepOpen: (() -> Void)?
    var onMoveFinished: (() -> Void)?
    var onSelectSession: ((String) -> Void)?
    var onAttribute: (() -> Bool)?
    var onNewSessionSample: (() -> Void)?
    var onSetAuto: ((Bool) -> Void)?
    var onCancelAttribution: (() -> Void)?
    var onFollow: (() -> Void)?
    var onThemeChange: ((MonitorTheme) -> Void)?
    var onDragChanged: ((Bool) -> Void)?
    var onMenuChanged: ((Bool) -> Void)?
    private var startMouse = NSPoint.zero
    private var startOrigin = NSPoint.zero
    private var moved = false
    private let scroller = NSScrollView()
    private let body = MonitorBodyView()
    private let sessionButton = NSButton()
    private var submitting = false
    private var submissionRestore: AttributionSnapshot?

    override var isFlipped: Bool { true }
    override var acceptsFirstResponder: Bool { false }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        setAccessibilityElement(true)
        setAccessibilityRole(.button)
        updateAccessibility()
        scroller.drawsBackground = false
        scroller.hasVerticalScroller = true
        scroller.autohidesScrollers = true
        scroller.scrollerStyle = .overlay
        scroller.borderType = .noBorder
        body.owner = self
        scroller.documentView = body
        addSubview(scroller)
        sessionButton.target = self
        sessionButton.action = #selector(chooseSession(_:))
        sessionButton.alignment = .left
        sessionButton.isBordered = false
        sessionButton.font = .systemFont(ofSize: 11.5)
        addSubview(sessionButton)
        refreshControls()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    private func updateAccessibility() {
        let session = state.sessionTitle ?? "未命名会话"
        setAccessibilityRole(collapsed ? .button : .group)
        setAccessibilityLabel("\(AppBrand.displayName)，\(state.label)，\(state.bindingLabel)：\(session)")
        setAccessibilityHelp(collapsed ? "单击展开，右键打开系统菜单，拖动移动并吸边。" : "单击空白处保持展开，减号收起；查看所选配置和数字参考，右键打开系统菜单。")
        toolTip = "\(state.label)\n会话：\(session)\n会话 ID：\(state.sessionID ?? "未发现")\n\(state.configurationLabel)：\(state.samplingModel ?? "未记录")\n返回模型：\(state.referenceValue)\n范围：\(state.attribution.scopeLabel)\n数字指纹参考仅代表本次采样。"
        if let error = state.commandError ?? (state.status == "error" ? state.detail : nil) { toolTip = (toolTip ?? "") + "\n" + error }

    }

    override func accessibilityPerformPress() -> Bool {
        onKeepOpen?()
        return true
    }

    private var palette: MonitorPalette { MonitorPalette(dark: theme == .graphite) }
    private var collapseButton: NSRect { NSRect(x: bounds.width - 55, y: 21, width: 30, height: 30) }
    var preferredExpandedWidth: CGFloat { 360 }
    var preferredExpandedHeight: CGFloat { 320 }

    override func layout() {
        super.layout()
        sessionButton.frame = NSRect(x: 25, y: 117, width: bounds.width - 50, height: 27)
        scroller.frame = NSRect(x: 12, y: 155, width: bounds.width - 24, height: max(1, bounds.height - 192))
        body.frame = NSRect(x: 0, y: 0, width: scroller.contentSize.width, height: 128)
    }

    private func refreshControls() {
        let p = palette
        appearance = NSAppearance(named: theme == .graphite ? .darkAqua : .aqua)
        scroller.isHidden = collapsed
        sessionButton.isHidden = collapsed
        sessionButton.title = "\(state.sessionTitle ?? "选择监测会话")  ▾"
        sessionButton.toolTip = "按名称绑定监测会话。\n会话 ID：\(state.sessionID ?? "未发现")"
        for button in [sessionButton] {
            button.contentTintColor = p.ink
            button.appearance = appearance
            button.attributedTitle = NSAttributedString(string: button.title, attributes: [
                .font: button.font ?? NSFont.systemFont(ofSize: 12),
                .foregroundColor: button.isEnabled ? p.ink : p.muted
            ])
        }
        body.toolTip = toolTip
        needsLayout = true
        body.needsDisplay = true
    }

    private func submitManualAttribution() {
        guard state.canAttribute, !submitting else { return }
        if state.sessionID == nil && state.requiresNewSample { onNewSessionSample?(); return }
        if onAttribute?() == true {
            if !submitting { markMenuSubmissionPending() }
        } else { clearMenuSubmissionPending() }
    }
    func markMenuSubmissionPending() {
        let previous = state.attribution
        var pending = state
        pending.attribution.status = "queued"
        pending.attribution.taskID = nil
        pending.attribution.taskSource = "manual"
        pending.attribution.confirmation = nil
        pending.attribution.currentCandidate = nil
        pending.attribution.previewLabel = nil
        pending.attribution.cancellable = false
        pending.attribution.progress = "正在提交归因"
        state = pending
        submissionRestore = previous
        submitting = true
        refreshControls()
    }
    func clearMenuSubmissionPending() {
        let previous = submissionRestore
        submissionRestore = nil
        submitting = false
        if let previous { state.attribution = previous }
        refreshControls()
    }

    @objc private func chooseSession(_ sender: NSButton) {
        let menu = NSMenu(title: "选择监测会话")
        let follow = NSMenuItem(title: "跟随最近活跃会话", action: #selector(sessionChosen(_:)), keyEquivalent: "")
        follow.target = self
        follow.representedObject = ""
        follow.state = state.selectionMode == "recent_activity" ? .on : .off
        menu.addItem(follow)
        menu.addItem(.separator())
        if state.sessions.isEmpty {
            let empty = NSMenuItem(title: "尚未发现可读取的会话", action: nil, keyEquivalent: "")
            empty.isEnabled = false
            menu.addItem(empty)
        }
        for session in state.sessions {
            let item = NSMenuItem(title: session.title, action: #selector(sessionChosen(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = session.id
            item.toolTip = "会话 ID：\(session.id)"
            item.state = state.selectionMode == "bound" && state.sessionID == session.id ? .on : .off
            menu.addItem(item)
        }
        menu.minimumWidth = 220
        let point = window?.convertPoint(toScreen: convert(NSPoint(x: sender.frame.minX, y: sender.frame.maxY + 3), to: nil)) ?? NSEvent.mouseLocation
        onMenuChanged?(true)
        defer { onMenuChanged?(false) }
        menu.popUp(positioning: menu.items.first(where: \.isSeparatorItem), at: point, in: nil)
    }
    @objc private func sessionChosen(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        if id.isEmpty { onFollow?() } else { onSelectSession?(id) }
    }

    private func rounded(_ rect: NSRect, radius: CGFloat, fill: NSColor, stroke: NSColor? = nil) {
        let path = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
        fill.setFill()
        path.fill()
        if let stroke { stroke.setStroke(); path.lineWidth = 0.8; path.stroke() }
    }

    private func gradient(_ rect: NSRect, radius: CGFloat, colors: [NSColor], angle: CGFloat = 90) {
        let path = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
        NSGradient(colors: colors)?.draw(in: path, angle: angle)
    }

    private func line(y: CGFloat, left: CGFloat = 26, right: CGFloat? = nil, color: NSColor? = nil) {
        let path = NSBezierPath()
        path.move(to: NSPoint(x: left, y: y))
        path.line(to: NSPoint(x: right ?? bounds.width - 26, y: y))
        (color ?? palette.line).setStroke()
        path.lineWidth = 0.75
        path.stroke()
    }

    private func text(_ value: String, rect: NSRect, size: CGFloat, color: NSColor,
                      weight: NSFont.Weight = .regular, alignment: NSTextAlignment = .left,
                      wrap: Bool = false, mono: Bool = false,
                      truncation: NSLineBreakMode = .byTruncatingMiddle) {
        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = alignment
        paragraph.lineBreakMode = wrap ? .byWordWrapping : truncation
        paragraph.lineSpacing = 3
        let font = mono ? NSFont.monospacedSystemFont(ofSize: size, weight: weight) : NSFont.systemFont(ofSize: size, weight: weight)
        let attributes: [NSAttributedString.Key: Any] = [
            .font: font, .foregroundColor: color, .paragraphStyle: paragraph
        ]
        (value as NSString).draw(in: rect, withAttributes: attributes)
    }

    var statusColor: NSColor {
        switch state.numericStatus {
        case "close": return palette.green
        case "confirming", "different", "error", "inconclusive": return palette.amber
        default: return palette.blue
        }
    }

    var statusSymbol: String {
        switch state.numericStatus {
        case "close": return "✓"
        case "confirming", "different", "error", "inconclusive": return "!"
        case "running": return "…"
        default: return "·"
        }
    }

    private func statusMedallion(in rect: NSRect, symbol: String, color: NSColor, badge: Bool = false) {
        let p = palette
        let inset: CGFloat = badge ? 2 : 0
        if badge {
            gradient(rect, radius: rect.width / 2,
                     colors: p.dark ? [rgb(0.82, 0.91, 1), rgb(0.29, 0.45, 0.64)] : [rgb(1, 1, 1), rgb(0.68, 0.79, 0.96)])
        }
        let face = rect.insetBy(dx: inset, dy: inset)
        gradient(face, radius: face.width / 2,
                 colors: [color.blended(withFraction: 0.12, of: .white) ?? color, color.blended(withFraction: 0.18, of: .black) ?? color])
        let glyph = p.dark ? rgb(0.06, 0.16, 0.28) : NSColor.white
        text(symbol, rect: NSRect(x: face.minX, y: face.minY + (face.height - 20) / 2 - 1, width: face.width, height: 22),
             size: 16, color: glyph, weight: .bold, alignment: .center)
    }

    private func drawLogo(in rect: NSRect) {
        if let icon = AppBrand.icon {
            NSGraphicsContext.saveGraphicsState()
            NSBezierPath(ovalIn: rect).addClip()
            icon.draw(in: rect, from: .zero, operation: .sourceOver, fraction: 1, respectFlipped: true, hints: [.interpolation: NSImageInterpolation.high])
            NSGraphicsContext.restoreGraphicsState()
            return
        }
        let p = palette
        gradient(rect, radius: rect.width / 2, colors: [p.blue, p.blueBack])
        text("B", rect: rect.insetBy(dx: 3, dy: 1), size: 17, color: p.ink, weight: .bold, alignment: .center)
    }

    private func orbGradient(_ rect: NSRect, colors: [NSColor]) {
        NSGraphicsContext.saveGraphicsState()
        NSBezierPath(ovalIn: rect).addClip()
        NSGradient(colors: colors)?.draw(from: NSPoint(x: rect.midX, y: rect.minY),
                                         to: NSPoint(x: rect.midX, y: rect.maxY), options: [])
        NSGraphicsContext.restoreGraphicsState()
    }

    // NSShadow offsets use screen base coordinates for a live window and
    // bitmap base coordinates for cacheDisplay. Normalize their vertical axis
    // so both exports and the actual desktop cast the shadow downwards.
    private var orbShadowYDirection: CGFloat {
        NSGraphicsContext.current?.isDrawingToScreen == true ? -1 : 1
    }

    private func orbGlyph(_ value: String, center: NSPoint, size: CGFloat, top: NSColor, bottom: NSColor,
                          shadowColor: NSColor, shadowBlur: CGFloat = 0.9) {
        let nativeFont = NSFont.systemFont(ofSize: size, weight: .semibold)
        let font = CTFontCreateWithName(nativeFont.fontName as CFString, size, nil)
        var characters = Array(value.utf16)
        var glyphs = [CGGlyph](repeating: 0, count: characters.count)
        guard CTFontGetGlyphsForCharacters(font, &characters, &glyphs, characters.count) else { return }
        var advances = [CGSize](repeating: .zero, count: glyphs.count)
        CTFontGetAdvancesForGlyphs(font, .horizontal, glyphs, &advances, glyphs.count)
        let outlines = CGMutablePath()
        var x: CGFloat = 0
        for (index, glyph) in glyphs.enumerated() {
            if let outline = CTFontCreatePathForGlyph(font, glyph, nil) {
                outlines.addPath(outline, transform: CGAffineTransform(translationX: x, y: 0))
            }
            x += advances[index].width
        }
        let box = outlines.boundingBoxOfPath
        var placement = CGAffineTransform(a: 1, b: 0, c: 0, d: -1,
                                          tx: center.x - box.midX, ty: center.y + box.midY)
        guard let letters = outlines.copy(using: &placement), let context = NSGraphicsContext.current?.cgContext else { return }
        NSGraphicsContext.saveGraphicsState()
        let shadow = NSShadow()
        shadow.shadowOffset = NSSize(width: 0.25, height: 0.65 * orbShadowYDirection)
        shadow.shadowBlurRadius = shadowBlur
        shadow.shadowColor = shadowColor
        shadow.set()
        context.addPath(letters)
        context.setFillColor(bottom.cgColor)
        context.fillPath()
        NSGraphicsContext.restoreGraphicsState()
        // The small lower extrusion and face gradient reproduce the raised cool-white numerals.
        NSGraphicsContext.saveGraphicsState()
        context.translateBy(x: 0, y: 0.38)
        context.addPath(letters)
        context.setFillColor(bottom.blended(withFraction: 0.23, of: .black)!.cgColor)
        context.fillPath()
        NSGraphicsContext.restoreGraphicsState()
        NSGraphicsContext.saveGraphicsState()
        context.addPath(letters)
        context.clip()
        let face = letters.boundingBoxOfPath
        NSGradient(starting: top, ending: bottom)?.draw(from: NSPoint(x: face.midX, y: face.minY),
                                                       to: NSPoint(x: face.midX, y: face.maxY), options: [])
        NSGraphicsContext.restoreGraphicsState()
    }

    private func drawOrbBadge(center: NSPoint, diameter: CGFloat) {
        let dark = theme == .graphite
        let rect = NSRect(x: center.x - diameter / 2, y: center.y - diameter / 2,
                          width: diameter, height: diameter)
        let symbol = statusSymbol
        let warning = ["confirming", "different", "error", "inconclusive"].contains(state.numericStatus)
        let matched = state.numericStatus == "close"
        NSGraphicsContext.saveGraphicsState()
        let shadow = NSShadow()
        shadow.shadowColor = rgb(0.04, 0.10, 0.20, dark ? 0.34 : 0.22)
        shadow.shadowOffset = NSSize(width: 0.7, height: 1.4 * orbShadowYDirection)
        shadow.shadowBlurRadius = 2.0
        shadow.set()
        rgb(0.75, 0.85, 0.96).setFill()
        NSBezierPath(ovalIn: rect).fill()
        NSGraphicsContext.restoreGraphicsState()
        orbGradient(rect, colors: [rgb(0.95, 0.98, 1), rgb(0.77, 0.87, 0.97)])
        let face = rect.insetBy(dx: diameter * 0.08, dy: diameter * 0.08)
        let colors: [NSColor]
        if warning {
            colors = dark ? [rgb(0.33, 0.22, 0.09), rgb(0.49, 0.33, 0.14)] : [rgb(0.88, 0.61, 0.27), rgb(0.98, 0.77, 0.43)]
        } else if matched {
            colors = dark ? [rgb(0.12, 0.28, 0.29), rgb(0.17, 0.41, 0.38)] : [rgb(0.25, 0.59, 0.46), rgb(0.40, 0.72, 0.59)]
        } else {
            colors = dark ? [rgb(0.13, 0.23, 0.38), rgb(0.215, 0.40, 0.665)] :
                            [rgb(0.39, 0.55, 0.82), rgb(0.51, 0.65, 0.89)]
        }
        orbGradient(face, colors: colors)
        if dark && !warning && !matched {
            NSGraphicsContext.saveGraphicsState()
            NSBezierPath(ovalIn: face).addClip()
            NSGradient(starting: rgb(0.03, 0.08, 0.18, 0.36), ending: rgb(0.03, 0.08, 0.18, 0))?
                .draw(from: NSPoint(x: face.minX, y: face.midY), to: NSPoint(x: face.midX, y: face.midY),
                      options: [.drawsAfterEndingLocation])
            NSGraphicsContext.restoreGraphicsState()
        }
        let rim = NSBezierPath(ovalIn: face.insetBy(dx: 0.25, dy: 0.25))
        rgb(0.69, 0.83, 0.98, 0.55).setStroke()
        rim.lineWidth = 0.4
        rim.stroke()
        let glyphColor = dark ? (warning ? rgb(1, 0.82, 0.50) : matched ? rgb(0.72, 0.97, 0.86) : rgb(0.56, 0.82, 1)) : .white
        orbGlyph(symbol, center: NSPoint(x: center.x, y: center.y + 0.1), size: diameter * 0.64,
                 top: glyphColor, bottom: glyphColor, shadowColor: rgb(0.02, 0.06, 0.15, 0.18), shadowBlur: 0.35)
    }

    private func drawOrb() {
        let dark = theme == .graphite
        let diameter: CGFloat = 76
        let orb = NSRect(x: 10, y: 7, width: diameter, height: diameter)
        // Paint a soft contact halo and a separate downward cast shadow. Both
        // are real alpha outside the sphere, rather than a glow inside its rim.
        NSGraphicsContext.saveGraphicsState()
        let ambient = NSShadow()
        ambient.shadowOffset = .zero
        ambient.shadowBlurRadius = 5.8
        ambient.shadowColor = rgb(0.17, 0.29, 0.49, dark ? 0.24 : 0.17)
        ambient.set()
        rgb(0.09, 0.13, 0.19).setFill()
        NSBezierPath(ovalIn: orb).fill()
        NSGraphicsContext.restoreGraphicsState()
        NSGraphicsContext.saveGraphicsState()
        let shadow = NSShadow()
        shadow.shadowOffset = NSSize(width: 3.0, height: 6.5 * orbShadowYDirection)
        shadow.shadowBlurRadius = 10.0
        shadow.shadowColor = rgb(0.11, 0.20, 0.35, dark ? 0.48 : 0.36)
        shadow.set()
        rgb(0.09, 0.13, 0.19).setFill()
        NSBezierPath(ovalIn: orb).fill()
        NSGraphicsContext.restoreGraphicsState()

        if dark {
            orbGradient(orb, colors: [rgb(0.18, 0.25, 0.34), rgb(0.12, 0.18, 0.26), rgb(0.16, 0.22, 0.31)])
            orbGradient(orb.insetBy(dx: 0.7, dy: 0.7), colors: [rgb(0.79, 0.855, 0.93), rgb(0.28, 0.40, 0.59), rgb(0.39, 0.52, 0.69)])
            orbGradient(orb.insetBy(dx: 1.45, dy: 1.45), colors: [rgb(0.11, 0.18, 0.28), rgb(0.08, 0.13, 0.21)])
            orbGradient(orb.insetBy(dx: 1.65, dy: 1.65), colors: [rgb(0.565, 0.694, 0.898), rgb(0.298, 0.427, 0.655), rgb(0.329, 0.471, 0.651)])
            let face = orb.insetBy(dx: 3.0, dy: 3.0)
            orbGradient(face, colors: [rgb(0.13, 0.18, 0.255), rgb(0.090, 0.114, 0.153), rgb(0.070, 0.103, 0.145)])
            NSGraphicsContext.saveGraphicsState()
            NSBezierPath(ovalIn: face).addClip()
            let lightCenter = NSPoint(x: orb.minX + diameter * 0.20, y: orb.minY + diameter * 0.08)
            NSGradient(starting: rgb(0.337, 0.498, 0.722, 0.75), ending: rgb(0.337, 0.498, 0.722, 0))?
                .draw(fromCenter: lightCenter, radius: 0, toCenter: lightCenter, radius: diameter * 0.33,
                      options: [.drawsAfterEndingLocation])
            NSGraphicsContext.restoreGraphicsState()
            // A restrained bottom bounce is confined to the face edge, leaving its center dark.
            NSGraphicsContext.saveGraphicsState()
            NSBezierPath(ovalIn: face).addClip()
            NSGradient(starting: rgb(0.25, 0.39, 0.59, 0.50), ending: rgb(0.25, 0.39, 0.59, 0))?
                .draw(fromCenter: NSPoint(x: orb.midX + 4, y: orb.maxY + 5), radius: 0,
                      toCenter: NSPoint(x: orb.midX + 4, y: orb.maxY + 5), radius: 26,
                      options: [.drawsAfterEndingLocation])
            NSGraphicsContext.restoreGraphicsState()
            orbGlyph(state.badge, center: NSPoint(x: orb.midX, y: orb.midY + 0.3),
                     size: state.badge.count > 3 ? 26 : 29, top: rgb(0.975, 0.99, 1), bottom: rgb(0.82, 0.88, 0.93),
                     shadowColor: rgb(0, 0, 0, 0.48))
        } else {
            orbGradient(orb, colors: [rgb(0.65, 0.73, 0.96), rgb(0.70, 0.80, 0.96), rgb(0.79, 0.86, 0.98)])
            orbGradient(orb.insetBy(dx: 0.7, dy: 0.7), colors: [rgb(0.98, 0.99, 1), rgb(0.91, 0.95, 1), rgb(0.94, 0.97, 1)])
            let face = orb.insetBy(dx: 1.5, dy: 1.5)
            orbGradient(face, colors: [rgb(0.96, 0.977, 0.997), rgb(0.89, 0.925, 0.98), rgb(0.867, 0.902, 0.965)])
            NSGraphicsContext.saveGraphicsState()
            NSBezierPath(ovalIn: face).addClip()
            NSGradient(starting: rgb(1, 1, 1, 0.44), ending: rgb(1, 1, 1, 0))?
                .draw(fromCenter: NSPoint(x: orb.minX + diameter * 0.25, y: orb.minY + diameter * 0.20), radius: 0,
                      toCenter: NSPoint(x: orb.minX + diameter * 0.25, y: orb.minY + diameter * 0.20), radius: diameter * 0.70,
                      options: [.drawsAfterEndingLocation])
            NSGraphicsContext.restoreGraphicsState()
            orbGlyph(state.badge, center: NSPoint(x: orb.midX, y: orb.midY + 0.3),
                     size: state.badge.count > 3 ? 26 : 29, top: rgb(0.045, 0.085, 0.19), bottom: rgb(0.075, 0.12, 0.25),
                     shadowColor: rgb(0.24, 0.34, 0.55, 0.12), shadowBlur: 0.45)
        }
        drawOrbBadge(center: NSPoint(x: orb.midX + diameter * 82 / 224, y: orb.midY + diameter * 74 / 224),
                     diameter: diameter * 58 / 224)
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        if collapsed { drawOrb(); return }
        let p = palette
        let card = bounds.insetBy(dx: 6, dy: 6)
        gradient(card, radius: 19, colors: [p.top, p.bottom], angle: 72)
        let rim = NSBezierPath(roundedRect: card, xRadius: 19, yRadius: 19)
        p.rim.setStroke(); rim.lineWidth = 0.9; rim.stroke()
        let inner = NSBezierPath(roundedRect: card.insetBy(dx: 1.4, dy: 1.4), xRadius: 17.5, yRadius: 17.5)
        p.innerRim.setStroke(); inner.lineWidth = 0.9; inner.stroke()
        let highlight = NSBezierPath()
        highlight.move(to: NSPoint(x: 26, y: card.minY + 2.1))
        highlight.line(to: NSPoint(x: bounds.width - 26, y: card.minY + 2.1))
        NSColor.white.withAlphaComponent(p.dark ? 0.10 : 0.65).setStroke()
        highlight.lineWidth = 0.5; highlight.stroke()
        drawLogo(in: NSRect(x: 26, y: 25, width: 26, height: 25))
        text(AppBrand.displayName, rect: NSRect(x: 65, y: 26, width: 180, height: 25), size: 18, color: p.ink, weight: .semibold)
        rounded(collapseButton, radius: 9, fill: p.button, stroke: p.line)
        let minus = NSBezierPath()
        minus.move(to: NSPoint(x: collapseButton.minX + 10, y: collapseButton.midY))
        minus.line(to: NSPoint(x: collapseButton.maxX - 10, y: collapseButton.midY))
        p.muted.setStroke(); minus.lineWidth = 1.7; minus.lineCapStyle = .round; minus.stroke()
        let warning = ["confirming", "different", "error", "inconclusive"].contains(state.numericStatus)
        rounded(NSRect(x: 25, y: 65, width: bounds.width - 50, height: 42), radius: 11,
                fill: warning ? p.amberBack : p.blueBack, stroke: warning ? p.amberRim : p.blueRim)
        statusMedallion(in: NSRect(x: 38, y: 75, width: 20, height: 20), symbol: statusSymbol, color: statusColor)
        text(state.label, rect: NSRect(x: 70, y: 75, width: bounds.width - 108, height: 24), size: 14, color: statusColor, weight: .semibold)
        text(state.automaticStatus, rect: NSRect(x: 27, y: bounds.height - 30, width: bounds.width - 54, height: 16), size: 9.5, color: p.quiet)
    }

    private func drawInformationBody() {
        let p = palette
        let w = body.bounds.width
        let modelX: CGFloat = 86
        text(state.configurationLabel, rect: NSRect(x: 15, y: 13, width: 65, height: 21), size: 12, color: p.muted)
        text(state.samplingModel ?? "未记录", rect: NSRect(x: modelX, y: 11, width: w - modelX - 15, height: 24),
             size: 13, color: state.samplingModel == nil ? p.muted : p.ink, weight: .medium, mono: state.samplingModel != nil)
        let a = state.attribution
        text(state.attributionRowLabel, rect: NSRect(x: 15, y: 56, width: 65, height: 21), size: 12, color: p.muted)
        text(state.referenceValue, rect: NSRect(x: modelX, y: 54, width: w - modelX - 15, height: 25),
             size: 12.5, color: a.result == nil ? p.quiet : a.warning ? p.amber : p.ink, weight: .medium, mono: a.busy ? a.currentCandidate != nil : a.attributedModel != nil)
        line(y: 91, left: 15, right: w - 15)
        let footer = AttributionFooterLayout(state: state, width: w)
        if let summary = footer.summary {
            text(summary, rect: footer.summaryRect, size: footer.detection == nil ? 10 : 9.5,
                 color: state.numericStatus == "error" ? p.amber : p.quiet, truncation: .byTruncatingTail)
        }
        if let detection = footer.detection, let rect = footer.detectionRect {
            text(detection, rect: rect, size: 9.5,
                 color: state.numericStatus == "error" ? p.amber : p.quiet,
                 alignment: footer.detectionAlignment, truncation: .byTruncatingHead)
        }
    }

    fileprivate func drawBody() { drawInformationBody() }

    override func mouseDown(with event: NSEvent) {
        onDragChanged?(true)
        startMouse = window?.convertPoint(toScreen: event.locationInWindow) ?? event.locationInWindow
        startOrigin = window?.frame.origin ?? .zero
        moved = false
    }

    override func mouseDragged(with event: NSEvent) {
        let point = window?.convertPoint(toScreen: event.locationInWindow) ?? event.locationInWindow
        let dx = point.x - startMouse.x
        let dy = point.y - startMouse.y
        if hypot(dx, dy) > 3 { moved = true }
        if moved { window?.setFrameOrigin(NSPoint(x: startOrigin.x + dx, y: startOrigin.y + dy)) }
    }

    override func mouseUp(with event: NSEvent) {
        if moved {
                onMoveFinished?()
            onDragChanged?(false)
            return
        }
        let point = convert(event.locationInWindow, from: nil)
        if !collapsed && collapseButton.contains(point) { onToggle?() }
        else { onKeepOpen?() }
        onDragChanged?(false)
    }

    override func rightMouseDown(with event: NSEvent) {
        let point = window?.convertPoint(toScreen: event.locationInWindow) ?? NSEvent.mouseLocation
        DispatchQueue.main.async { [weak self] in self?.showOperationsMenu(at: point) }
    }

    func makeOperationsMenu() -> NSMenu {
        let menu = NSMenu(title: "模型灯塔")
        menu.minimumWidth = 220
        menu.autoenablesItems = false
        func item(_ title: String, _ tag: Int, enabled: Bool = true, state check: NSControl.StateValue = .off) -> NSMenuItem {
            let row = NSMenuItem(title: title, action: #selector(menuAction(_:)), keyEquivalent: "")
            row.target = self; row.tag = tag; row.isEnabled = enabled; row.state = check
            return row
        }
        let manual = item(submitting ? "正在排队…" : "手动数字归因", 101, enabled: state.canAttribute && !submitting)
        manual.representedObject = state.sessionID
        manual.toolTip = state.sessionID == nil && state.requiresNewSample ? "选择型号和提供方，采样独立临时会话。" : state.canAttribute ? "采样当前显示的‘\(state.sessionTitle ?? "未命名会话")’；每次初检 1 次，差异时再复测 3 次" : state.attributionReason
        menu.addItem(manual)
        let automatic = item("自动数字归因", 102, enabled: state.canSetAutomatic,
                             state: state.attribution.enabled ? .on : .off)
        automatic.representedObject = state.sessionID
        automatic.toolTip = "全局开关；切换会话后保持开启，先检查当前绑定会话。"
        menu.addItem(automatic)
        let newSession = item("新会话采样…", 104, enabled: onNewSessionSample != nil && !state.attribution.busy && !submitting)
        newSession.toolTip = "选择型号和提供方，采样独立临时会话。"
        menu.addItem(newSession)
        if state.attribution.busy {
            let cancel = item(state.attribution.status == "cancelling" ? "正在取消…" : "取消本次归因", 103,
                              enabled: state.attribution.status != "cancelling" && state.attribution.cancellable)
            cancel.representedObject = state.attribution.taskID
            menu.addItem(cancel)
        }
        menu.addItem(.separator())
        let a = state.attribution
        let history = NSMenuItem(title: "查看归因记录", action: nil, keyEquivalent: "")
        let records = NSMenu(title: "归因记录")
        records.autoenablesItems = false
        if a.busy {
            let progress = "进行中：" + a.progressSummary
            let row = item(String(progress.prefix(36)) + (progress.count > 36 ? "…" : ""), 0, enabled: false)
            records.addItem(row)
        }
        let independent = a.sampleScope == "new_session"
        let configuredModel = independent ? a.model : state.samplingModel
        let recordLines: [String] = a.result == nil ? (a.busy ? [] : ["尚无数字归因记录"]) : [
            a.busy ? (independent ? "上次新会话归因记录" : "上次归因记录") : (independent ? "新会话归因记录" : "数字归因记录"),
            "时间：" + readableTime(a.completedAt),
            "有效样本：\(a.validSamples)",
            "尝试次数：\(a.attempts)",
            "当前配置模型：\(configuredModel ?? "未记录")",
            "返回模型：" + (a.attributedModel ?? "未返回"),
            "原因：" + a.recordReason,
        ]
        for title in recordLines { records.addItem(item(title, 0, enabled: false)) }
        history.submenu = records; menu.addItem(history)
        let themeItem = NSMenuItem(title: "主题", action: nil, keyEquivalent: "")
        let themes = NSMenu(title: "主题"); themes.autoenablesItems = false
        for (index, option) in MonitorTheme.allCases.enumerated() {
            themes.addItem(item(option.title, 10 + index, state: theme == option ? .on : .off))
        }
        themeItem.submenu = themes; menu.addItem(themeItem)
        let sessionItem = NSMenuItem(title: "监测会话选择", action: nil, keyEquivalent: "")
        let sessions = NSMenu(title: "监测会话选择"); sessions.autoenablesItems = false
        sessions.addItem(item("跟随最近活跃会话", 3, state: state.selectionMode == "recent_activity" ? .on : .off))
        if !state.sessions.isEmpty { sessions.addItem(.separator()) }
        if state.sessions.isEmpty { sessions.addItem(item("尚未发现可读取的会话", 0, enabled: false)) }
        for session in state.sessions {
            let row = NSMenuItem(title: session.title, action: #selector(sessionChosen(_:)), keyEquivalent: "")
            row.target = self; row.representedObject = session.id
            row.toolTip = "会话 ID：\(session.id)"
            row.state = state.selectionMode == "bound" && state.sessionID == session.id ? .on : .off
            sessions.addItem(row)
        }
        sessionItem.submenu = sessions; menu.addItem(sessionItem)
        menu.addItem(.separator())
        menu.addItem(item("退出\(AppBrand.displayName)", 7))
        return menu
    }

    func showOperationsMenu(at point: NSPoint = NSEvent.mouseLocation) {
        onMenuChanged?(true)
        defer { onMenuChanged?(false) }
        let menu = makeOperationsMenu()
        // The release that opened the menu must land on a non-actionable row,
        // never on the first item that would send a model request.
        menu.popUp(positioning: menu.items.first(where: \.isSeparatorItem), at: point, in: nil)
    }

    @objc private func menuAction(_ sender: NSMenuItem) {
        if [101, 102].contains(sender.tag) {
            guard sender.representedObject as? String == state.sessionID else { return }
        }
        switch sender.tag {
        case 101: submitManualAttribution()
        case 102:
            guard state.canSetAutomatic else { return }
            let enabled = !state.attribution.enabled
            state.attribution.enabled = enabled
            sender.state = enabled ? .on : .off
            onSetAuto?(enabled)
        case 103:
            if state.attribution.cancellable, sender.representedObject as? String == state.attribution.taskID {
                state.attribution.status = "cancelling"
                state.attribution.cancellable = false
                state.attribution.previewLabel = nil
                state.attribution.progress = "正在取消归因"
                onCancelAttribution?()
            }
        case 104: onNewSessionSample?()
        case 3: onFollow?()
        case 7: NSApp.terminate(nil)
        case 10: onThemeChange?(.graphite)
        case 11: onThemeChange?(.silver)
        default: break
        }
    }

}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var panel: FloatingPanel!
    private var monitorView: MonitorView!
    private var reader = PassiveReader()
    private let preferences = UserDefaults.standard
    private let collapsedSize = NSSize(width: 104, height: 104)
    private var manualSubmissionPending = false
    private var submissionTimeout: DispatchWorkItem?
    private var previewing = false
    private var selectedSession: String?
    private var pendingBindingRequest = false
    private var launchBinding: String?
    private var seenAttributions: [String] = []
    private var readerGeneration = UUID()
    private var orbAnchor = NSRect.zero
    private var appliedPanelFrame = NSRect.zero
    private var dragging = false
    private var menuOpen = false
    private var displayObserver: NSObjectProtocol?
    private var pendingResultExpansion = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        let arguments = CommandLine.arguments
        previewing = arguments.contains("--preview-json") || arguments.contains("--snapshot")
        let storedTheme = previewing ? nil : preferences.string(forKey: "appearanceTheme")
        var initialTheme = storedTheme.flatMap(MonitorTheme.init(rawValue:)) ?? .graphite
        if let index = arguments.firstIndex(of: "--theme"), arguments.indices.contains(index + 1),
           let argumentTheme = MonitorTheme(rawValue: arguments[index + 1]) {
            initialTheme = argumentTheme
        }
        if let index = arguments.firstIndex(of: "--session"), arguments.indices.contains(index + 1) {
            selectedSession = arguments[index + 1]
        } else if !previewing {
            let environment = ProcessInfo.processInfo.environment
            selectedSession = environment["MODEL_BEACON_SESSION"] ?? environment["CODEX_THREAD_ID"]
        }
        if let launchBinding { selectedSession = launchBinding }
        let collapsed = arguments.contains("--expanded") ? false :
            (previewing ? true : preferences.object(forKey: "collapsed") as? Bool ?? true)
        let size = collapsed ? collapsedSize : NSSize(width: 360, height: 320)
        let visible = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1000, height: 750)
        var origin = NSPoint(x: visible.maxX - collapsedSize.width - 26, y: visible.minY + 60)
        if !previewing, preferences.object(forKey: "orbAnchorX") != nil {
            origin = NSPoint(x: preferences.double(forKey: "orbAnchorX"), y: preferences.double(forKey: "orbAnchorY"))
        } else if !previewing, preferences.object(forKey: "windowX") != nil {
            // Migrate the former window coordinate once. Subsequent expand /
            // collapse cycles only persist the independent collapsed anchor.
            origin = NSPoint(x: preferences.double(forKey: "windowX"), y: preferences.double(forKey: "windowY"))
        }
        orbAnchor = NSRect(origin: origin, size: collapsedSize)
        let anchorScreen = FloatingGeometry.screen(for: orbAnchor, among: visibleScreens)
        orbAnchor = FloatingGeometry.constrained(orbAnchor, to: anchorScreen)
        let initialFrame = collapsed ? orbAnchor : FloatingGeometry.expanded(around: orbAnchor, size: size, in: anchorScreen)
        panel = FloatingPanel(contentRect: initialFrame,
                              styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.level = .floating
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = !collapsed
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary]
        panel.isExcludedFromWindowsMenu = true
        panel.title = "\(AppBrand.productName) · \(AppBrand.displayName)"
        panel.delegate = self
        monitorView = MonitorView(frame: NSRect(origin: .zero, size: size))
        monitorView.theme = initialTheme
        monitorView.collapsed = collapsed
        monitorView.lockedSession = selectedSession
        monitorView.onToggle = { [weak self] in self?.collapseInfo() }
        monitorView.onKeepOpen = { [weak self] in self?.expandInfo() }
        monitorView.onSelectSession = { [weak self] id in self?.selectSession(id) }
        monitorView.onAttribute = { [weak self] in self?.requestManualAttribution() ?? false }
        monitorView.onNewSessionSample = { [weak self] in self?.showNewSampleDialog() }
        monitorView.onSetAuto = { [weak self] enabled in
            guard let self, !self.previewing else { return }
            guard let command = self.monitorView.state.automaticCommand(enabled: enabled) else { return }
            if !self.reader.send(command) {
                var state = self.monitorView.state
                state.attribution.enabled = !enabled
                state.commandError = "自动监测设置未发送，请重新打开应用后重试。"
                self.monitorView.state = state
            }
        }
        monitorView.onCancelAttribution = { [weak self] in
            guard let self, !self.previewing else { return }
            self.reader.send(self.monitorView.state.cancelAttributionCommand)
        }
        monitorView.onMoveFinished = { [weak self] in self?.moveFinished() }
        monitorView.onDragChanged = { [weak self] dragging in
            guard let self else { return }
            self.dragging = dragging
            if !dragging {
                if self.pendingResultExpansion { self.openResult() }
                self.applySize()
            }
        }
        monitorView.onMenuChanged = { [weak self] open in
            guard let self else { return }
            self.menuOpen = open
            if !open {
                if self.pendingResultExpansion { self.openResult() }
                self.applySize()
            }
        }
        monitorView.onFollow = { [weak self] in self?.selectSession(nil) }
        monitorView.onThemeChange = { [weak self] theme in
            guard let self else { return }
            self.monitorView.theme = theme
            if !self.previewing { self.preferences.set(theme.rawValue, forKey: "appearanceTheme") }
        }
        panel.contentView = monitorView
        applySize()
        saveAnchor()
        panel.orderFrontRegardless()
        if !previewing {
            displayObserver = NotificationCenter.default.addObserver(
                forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main
            ) { [weak self] _ in self?.applySize(); self?.saveAnchor() }
        }

        if let index = arguments.firstIndex(of: "--preview-json"), arguments.indices.contains(index + 1) {
            if let data = try? Data(contentsOf: URL(fileURLWithPath: arguments[index + 1])),
               let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
                receive(ModelSnapshot(json: json), autoExpand: false)
            } else { monitorView.state = .failure("无法读取预览状态文件。") }
        } else if !arguments.contains("--snapshot") {
            restartReader()
        }

        if let index = arguments.firstIndex(of: "--snapshot"), arguments.indices.contains(index + 1) {
            let outputPath = arguments[index + 1]
            DispatchQueue.main.async { [weak self] in self?.saveSnapshot(to: outputPath) }
        }
        if previewing && arguments.contains("--preview-native-menu") {
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.monitorView.showOperationsMenu(at: NSPoint(x: self.panel.frame.midX, y: self.panel.frame.midY))
            }
        }
    }

    private func requestManualAttribution() -> Bool {
        guard !previewing, !manualSubmissionPending, monitorView.state.canAttribute,
              let session = monitorView.state.sessionID else { return false }
        return submitSamplingCommand(["action": "attribute", "source": "manual", "session_id": session])
    }

    private func showNewSampleDialog() {
        guard !previewing, !manualSubmissionPending, !monitorView.state.attribution.busy else { return }
        let form = NewSessionSampleForm(state: monitorView.state)
        let alert = NSAlert()
        alert.messageText = "新会话采样"
        alert.informativeText = "初检 1 次，出现差异后复测 3 次"
        alert.alertStyle = .informational
        alert.addButton(withTitle: "开始采样")
        alert.addButton(withTitle: "取消")
        alert.accessoryView = form
        if let icon = AppBrand.icon { alert.icon = icon }
        alert.window.appearance = monitorView.appearance
        alert.window.initialFirstResponder = form.modelField
        NSApp.activate(ignoringOtherApps: true)
        while alert.runModal() == .alertFirstButtonReturn {
            alert.window.makeFirstResponder(nil)
            guard let command = form.command else {
                alert.informativeText = "请填写有效型号和提供方；初检 1 次，出现差异后复测 3 次。"
                continue
            }
            _ = submitSamplingCommand(command)
            return
        }
    }

    private func submitSamplingCommand(_ command: [String: Any]) -> Bool {
        guard !previewing, !manualSubmissionPending, !monitorView.state.attribution.busy else { return false }
        manualSubmissionPending = true
        monitorView.markMenuSubmissionPending()
        guard reader.send(command) else {
            manualSubmissionPending = false
            monitorView.clearMenuSubmissionPending()
            receive(.failure("本地读取组件未运行，归因请求未发送。"), autoExpand: false)
            return false
        }
        submissionTimeout?.cancel()
        let generation = readerGeneration
        let timeout = DispatchWorkItem { [weak self] in
            guard let self, self.readerGeneration == generation, self.manualSubmissionPending else { return }
            self.manualSubmissionPending = false
            self.monitorView.clearMenuSubmissionPending()
            var state = self.monitorView.state
            state.commandError = "归因请求未收到回执，请重试。"
            self.monitorView.state = state
        }
        submissionTimeout = timeout
        DispatchQueue.main.asyncAfter(deadline: .now() + 5, execute: timeout)
        return true
    }

    private func restartReader() {
        reader.stop()
        reader = PassiveReader()
        readerGeneration = UUID()
        let generation = readerGeneration
        reader.onUpdate = { [weak self] state in
            guard let self, self.readerGeneration == generation else { return }
            self.receive(state)
        }
        reader.start(session: selectedSession)
    }

    private func selectSession(_ session: String?, force: Bool = false) {
        guard force || selectedSession != session || (session != nil && monitorView.state.selectionMode != "bound") else { return }
        selectedSession = session
        monitorView.lockedSession = session
        pendingBindingRequest = true
        pendingResultExpansion = false
        monitorView.state = monitorView.state.switching(to: session)
        applySize()
        if !previewing {
            if let session { reader.send(["action": "bind", "session_id": session]) }
            else { reader.send(["action": "follow_recent"]) }
        }
    }

    // LaunchServices delivers this again to a running instance, unlike its
    // original process environment. A binding request never sends a model request.
    func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls {
            guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
                  parts.scheme?.lowercased() == "modelbeacon", parts.host == "bind",
                  parts.path.isEmpty || parts.path == "/", parts.fragment == nil,
                  parts.user == nil, parts.password == nil, parts.port == nil,
                  let items = parts.queryItems, items.count == 1, items[0].name == "session",
                  let id = items[0].value,
                  id.range(of: #"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"#, options: .regularExpression) != nil else { continue }
            if monitorView == nil { launchBinding = id }
            else { selectSession(id); panel.orderFrontRegardless() }
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        panel?.orderFrontRegardless()
        return true
    }

    private func receive(_ incomingState: ModelSnapshot, autoExpand: Bool = true) {
        var state = incomingState.retainingAutomaticSetting(from: monitorView.state)
        if state.status == "error", state.sessionID == nil {
            state.sessionID = selectedSession ?? monitorView.state.sessionID
            state.sessionTitle = monitorView.state.sessionTitle
            state.selectionMode = selectedSession == nil ? "recent_activity" : "bound"
            state.sessions = monitorView.state.sessions
        }
        if pendingBindingRequest {
            if let selectedSession {
                guard state.selectionMode == "bound", state.sessionID == selectedSession else { return }
            } else {
                guard state.selectionMode == "recent_activity" else { return }
            }
            pendingBindingRequest = false
        } else if let selectedSession, state.selectionMode == "bound", let receivedSession = state.sessionID, selectedSession != receivedSession { return }
        selectedSession = state.selectionMode == "bound" ? state.sessionID : nil
        monitorView.lockedSession = selectedSession
        if let before = monitorView.state.sessionID, let after = state.sessionID, before != after {
            pendingResultExpansion = false
        }
        manualSubmissionPending = false
        submissionTimeout?.cancel()
        submissionTimeout = nil
        monitorView.state = state
        let attribution = state.attribution
        if attribution.hasInitialDifference, attribution.confirmationStatus == "active", let taskID = attribution.taskID {
            let key = "initial-difference:" + taskID
            if !seenAttributions.contains(key) {
                seenAttributions.append(key)
                if autoExpand { openResult() }
            }
        }
        if attribution.result != nil, !attribution.restored, let completedAt = attribution.completedAt {
            let key = (attribution.result?["id"] as? String ?? attribution.taskSessionID ?? state.sessionID ?? "") + ":" + (attribution.sampleScope ?? "conversation") + ":" + completedAt
            if !seenAttributions.contains(key) {
                seenAttributions.append(key)
                if seenAttributions.count > 64 { seenAttributions.removeFirst(seenAttributions.count - 64) }
                if autoExpand { openResult() }
            }
        }
        applySize()
    }

    private func saveSnapshot(to outputPath: String) {
        let view = monitorView!
        view.layoutSubtreeIfNeeded()
        guard let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else {
            fputs("Cannot create snapshot bitmap.\n", stderr)
            exit(1)
        }
        view.cacheDisplay(in: view.bounds, to: bitmap)
        guard let png = bitmap.representation(using: .png, properties: [:]) else {
            fputs("Cannot encode snapshot PNG.\n", stderr)
            exit(1)
        }
        do {
            try png.write(to: URL(fileURLWithPath: outputPath), options: .atomic)
        } catch {
            fputs("Cannot save snapshot: \(error.localizedDescription)\n", stderr)
            exit(1)
        }
        NSApp.terminate(nil)
    }

    private var visibleScreens: [NSRect] { NSScreen.screens.map { $0.visibleFrame } }

    private func expandInfo() {
        monitorView.collapsed = false
        if !previewing { preferences.set(false, forKey: "collapsed") }
        applySize()
    }

    private func collapseInfo() {
        monitorView.collapsed = true
        if !previewing { preferences.set(true, forKey: "collapsed") }
        applySize()
    }

    private func openResult() {
        guard !dragging && !menuOpen else { pendingResultExpansion = true; return }
        monitorView.collapsed = false
        pendingResultExpansion = false
        if !previewing { preferences.set(false, forKey: "collapsed") }
    }

    private func applySize() {
        guard !dragging && !menuOpen else { return }
        let screen = FloatingGeometry.screen(for: orbAnchor, among: visibleScreens)
        orbAnchor = FloatingGeometry.constrained(orbAnchor, to: screen)
        // The orb paints its projection inside transparent padding. Expanded
        // cards additionally use the system's native window shadow.
        panel.hasShadow = !monitorView.collapsed
        let size = monitorView.collapsed ? collapsedSize : FloatingGeometry.panelSize(
            preferred: NSSize(width: monitorView.preferredExpandedWidth, height: monitorView.preferredExpandedHeight), in: screen)
        let frame = monitorView.collapsed ? orbAnchor : FloatingGeometry.expanded(around: orbAnchor, size: size, in: screen)
        if panel.frame != frame { panel.setFrame(frame, display: false) }
        appliedPanelFrame = frame
        if monitorView.frame.size != size { monitorView.frame = NSRect(origin: .zero, size: size) }
        monitorView.needsDisplay = true
    }

    private func moveFinished() {
        dragging = false
        let moved = panel.frame
        let screen = FloatingGeometry.screen(for: moved, among: visibleScreens)
        orbAnchor = FloatingGeometry.movedOrb(orbAnchor, previousPanel: appliedPanelFrame, movedPanel: moved, visible: screen)
        if pendingResultExpansion { openResult() }
        applySize()
        saveAnchor()
    }

    private func saveAnchor() {
        guard !previewing else { return }
        preferences.set(orbAnchor.minX, forKey: "orbAnchorX")
        preferences.set(orbAnchor.minY, forKey: "orbAnchorY")
        preferences.removeObject(forKey: "windowX")
        preferences.removeObject(forKey: "windowY")
    }

    func applicationWillTerminate(_ notification: Notification) {
        submissionTimeout?.cancel()
        if let displayObserver { NotificationCenter.default.removeObserver(displayObserver) }
        saveAnchor()
        reader.stop()
    }
    // This is an accessory application: AppKit's temporary menu window can
    // close while the nonactivating floating panel is still present. Only the
    // explicit Quit command should end the monitor.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
}

#if !MODEL_BEACON_MODEL_TESTS
@main
enum ModelBeaconApplication {
    static func main() {
        let application = NSApplication.shared
        let delegate = AppDelegate()
        application.setActivationPolicy(.accessory)
        application.delegate = delegate
        withExtendedLifetime(delegate) { application.run() }
    }
}

#endif
