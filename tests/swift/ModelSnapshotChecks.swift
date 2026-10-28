import Foundation
import AppKit

@main
struct ModelSnapshotChecks {
    static var count = 0
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        count += 1
        guard condition() else { fatalError("Snapshot regression: \(message)") }
    }
    static func main() {
        let configurationOnly = ModelSnapshot(json: [
            "schema_version": 2, "status": "ready", "session_id": "a", "selection_mode": "bound",
            "configuration_model": "gpt-6.1-sol", "configuration_effort": "high",
            "attribution": ["available": true, "enabled": false]
        ])
        check(configurationOnly.selectedModel == nil && configurationOnly.selectedEffort == nil,
              "turn configuration remains distinct from explicit selection")
        check(configurationOnly.samplingModel == "gpt-6.1-sol" && configurationOnly.configurationEffort == "high" && configurationOnly.configurationLabel == "采样配置",
              "numeric sampling can display the recorded configuration with an accurate source label")
        check(configurationOnly.label == "尚未归因" && configurationOnly.numericStatus == "idle", "unsampled conversations have a neutral numeric state")
        check(configurationOnly.canAttribute, "manual attribution remains available with automatic switch off")
        let recentActivity = ModelSnapshot(json: ["session_id": "a", "selection_mode": "recent_activity", "attribution": ["available": true]])
        check(recentActivity.canAttribute && recentActivity.canSetAutomatic,
              "an explicit action can sample or enable automatic attribution for the displayed recent session ID")
        let needsPreflight = ModelSnapshot(json: ["session_id": "a", "selection_mode": "recent_activity",
                                                 "manual_available": true, "auto_available": true,
                                                 "attribution": ["available": false]])
        check(needsPreflight.canAttribute && needsPreflight.canSetAutomatic,
              "metadata preflight keeps explicit actions available when local configuration is missing")
        let normalBusy = ModelSnapshot(json: ["session_id": "a", "busy": true, "turn_running": true,
                                              "manual_available": true, "auto_available": true])
        check(normalBusy.canAttribute && normalBusy.canSetAutomatic, "normal work never disables concurrent background sampling")
        let preparing = ModelSnapshot(json: ["session_id": "a", "manual_available": true,
                                             "attribution": ["status": "preparing"]])
        check(preparing.attribution.busy && !preparing.canAttribute && preparing.label == "正在准备采样",
              "metadata preflight acknowledges a real preparation task and prevents duplicates")
        let ignoredLegacy = ModelSnapshot(json: ["status": "mismatch", "requested_model": "gpt-6.1-sol", "actual_model": "gpt-5.6-sol",
                                                 "served_model": "gpt-5.6-sol", "actual_effort": "low",
                                                 "alert": ["id": "a"], "platform_history": [["result": "mismatch"]]])
        check(ignoredLegacy.label == "尚未归因" && ignoredLegacy.numericStatus == "idle",
              "retired platform fields cannot drive numeric state or unknown-model warnings")
        let close = ModelSnapshot(json: ["attribution": ["result": ["outcome": "close", "label": "接近所选模型",
                                             "model": "gpt-6.1-sol", "valid_samples": 1], "scope": "current"]])
        check(close.numericStatus == "close" && close.label == "归因匹配" && close.referenceValue == "gpt-6.1-sol", "successful attribution shows its sampled model directly")
        check(close.attributionRowLabel == "返回模型" && close.attribution.candidateLabel == "返回模型  gpt-6.1-sol",
              "card and records use a consistent attributed-model label")
        let different = ModelSnapshot(json: ["selected_model": "gpt-6.1-sol", "selected_effort": "ultra", "context_segment": 3,
            "attribution": ["result": ["outcome": "different", "label": "指纹有差异", "valid_samples": 3,
                                        "candidate": "gpt-5.6-sol"], "scope": "current"]])
        check(different.label == "指纹存在差异" && different.referenceValue == "gpt-5.6-sol",
              "numeric mismatch surfaces its candidate without claiming actual backend identity")
        check(different.contextSegment == "3" && different.configurationLabel == "所选型号", "selected configuration and context remain readable")
        let history = AttributionSnapshot(json: [
            "result": ["label": "接近所选模型"], "scope": "previous_config", "completed_at": 1_793_168_520.0
        ])
        check(history.scopeLabel == "上一配置的归因" && history.resultLabel == "归因匹配",
              "changed selection keeps the historical result in its original scope")
        check(history.completedAt != nil, "Unix completion timestamp is preserved")
        let restored = AttributionSnapshot(json: ["result": ["label": "接近所选模型"], "scope": "current", "restored": true])
        check(restored.scopeLabel == "历史归因记录", "restored results remain historical")
        let previousResult = ModelSnapshot(json: ["selected_model": "gpt-5.6-sol", "attribution": ["scope": "previous_turn",
            "completed_at": 1_793_168_520.0, "result": ["outcome": "close", "label": "接近所选模型", "model": "gpt-6.1-sol",
                "valid_samples": 1]]])
        check(previousResult.referenceValue == "gpt-6.1-sol" && previousResult.attribution.resultLabel == "归因模型 gpt-6.1-sol",
              "historical close results use their recorded model instead of the current selection")
        check(previousResult.attributionFooter.hasPrefix("上次检测 · ") && !previousResult.attributionFooter.contains("不代表") &&
              previousResult.attribution.scopeLabel == "上次归因，不代表本轮",
              "main card keeps a compact timestamp while detailed scope remains in records")
        let missingCandidateDifference = ModelSnapshot(json: ["selected_model": "gpt-6.1-sol", "attribution": ["result": [
            "outcome": "different", "label": "指纹有差异", "model": "gpt-6.1-sol", "valid_samples": 1]]])
        check(missingCandidateDifference.referenceValue == "未返回" && missingCandidateDifference.attribution.attributedModel == nil,
              "a missing returned candidate cannot reuse the requested model as a differing result")
        let missingBoundLog = ModelSnapshot(json: ["selection_mode": "bound", "bound_session_id": "a"])
        check(missingBoundLog.sessionID == "a" && !missingBoundLog.canAttribute, "missing logs retain binding without fabricating sampling readiness")
        let names = ModelSnapshot(json: ["sessions": [
            ["session_id": "a", "session_title": "同名"], ["session_id": "b", "session_title": "同名"]
        ]])
        check(names.sessions.map(\.id) == ["a", "b"], "same-title sessions retain distinct identifiers")
        let candidate = ModelSnapshot(json: ["attribution": ["scope": "current", "result": [
            "outcome": "inconclusive", "label": "未得出结论", "reference_supported": false,
            "valid_samples": 1, "candidate": "gpt-6-sol"]]])
        check(candidate.label == "已得出参考候选" && candidate.referenceValue == "gpt-6-sol" && candidate.attribution.resultLabel == "参考倾向 gpt-6-sol",
              "an inconclusive comparison retains its useful numeric reference candidate")
        let failedSample = AttributionSnapshot(json: ["result": ["label": "采样未完成", "reason_short": "请求超时",
                                                                  "valid_samples": 0, "attempts": 3]])
        check(failedSample.resultSummary == "采样未完成 · 请求超时" && failedSample.attempts == 3,
              "zero valid samples keep a concise actionable failure reason")
        let failed = ModelSnapshot(json: ["attribution": ["result": ["outcome": "inconclusive", "valid_samples": 0, "attempts": 3]]])
        check(failed.label == "采样未完成" && failed.numericStatus == "error", "sampling failure is distinct from an inconclusive valid comparison")
        let preparationFailure = ModelSnapshot(json: ["attribution": ["result": [
            "outcome": "inconclusive", "label": "准备采样失败", "valid_samples": 0, "attempts": 0,
            "failure_stage": "snapshot", "reason_short": "快照准备失败"]]])
        check(preparationFailure.label == "准备采样失败" && preparationFailure.numericStatus == "error" && preparationFailure.referenceValue == "快照准备失败",
              "a zero-request preparation failure shows its stage instead of implying an invalid model answer")
        check(preparationFailure.attribution.resultSummary == "准备采样失败 · 快照准备失败",
              "record summaries preserve preparation stage and zero-request distinction")
        let legacyPreparationFailure = ModelSnapshot(json: ["attribution": ["result": [
            "outcome": "inconclusive", "label": "采样未完成", "valid_samples": 0, "attempts": 0,
            "reason_short": "分支准备失败"]]])
        check(legacyPreparationFailure.label == "准备采样失败" && legacyPreparationFailure.referenceValue == "分支准备失败",
              "older zero-request histories are accurately presented as preparation failures")
        let partial = ModelSnapshot(json: ["session_id": "a", "attribution": [
            "status": "running", "progress": "采样2/3 · 有效1 · 83s", "preview_label": "参考倾向 gpt-5.6-sol",
            "phase": "model", "elapsed_seconds": 83.0,
            "task": ["id": "partial", "status": "running"]]])
        check(partial.attribution.progressSummary.contains("参考倾向 gpt-5.6-sol") && partial.attribution.elapsedSeconds == 83 && partial.attribution.phase == "model",
              "running tasks preserve intermediate reference and duration metadata")
        check(partial.label == "数字归因中" && !partial.canAttribute, "only an existing numeric task prevents duplicate manual submission")
        let runningWithOldResult = ModelSnapshot(json: ["attribution": [
            "current_candidate": "gpt-5.6-sol", "task": ["id": "current", "status": "running"],
            "result": ["outcome": "close", "candidate": "gpt-6.1-sol", "valid_samples": 1]]])
        check(runningWithOldResult.referenceValue == "gpt-5.6-sol" && runningWithOldResult.attribution.attributedModel == "gpt-6.1-sol",
              "running attribution shows the current candidate while completed records retain the prior result")
        let runningWithoutCandidate = ModelSnapshot(json: ["attribution": [
            "task": ["id": "new-initial", "status": "running"],
            "result": ["outcome": "close", "candidate": "gpt-6.1-sol", "valid_samples": 1]]])
        check(runningWithoutCandidate.referenceValue == "归因中", "a new task cannot reuse a stale completed model before its first response")
        let confirming = ModelSnapshot(json: ["session_id": "a", "attribution": [
            "task": ["id": "confirming", "status": "running"],
            "confirmation": ["initial_mismatch": true, "completed": 1, "required": 3, "status": "active"]]])
        check(confirming.numericStatus == "confirming" && confirming.label == "发现差异 · 复测中" && confirming.attribution.warning,
              "initial difference warns immediately during the three independent confirmations")
        let notConfirmed = ModelSnapshot(json: ["attribution": ["result": [
            "outcome": "inconclusive", "valid_samples": 4, "attempts": 4,
            "label": "复测未持续异常", "confirmation": ["initial_mismatch": true, "completed": 3, "status": "not_confirmed"]]]])
        check(notConfirmed.label == "复测未持续异常" && notConfirmed.attribution.hasInitialDifference,
              "a matching confirmation prevents escalation without losing the initial difference")
        let confirmed = ModelSnapshot(json: ["attribution": ["result": [
            "outcome": "different", "valid_samples": 4, "attempts": 4,
            "confirmation": ["initial_mismatch": true, "completed": 3, "status": "confirmed_mismatch"]]]])
        check(confirmed.label == "持续指纹差异", "only three mismatching confirmations escalate the current result")
        let oldConfirmed: [String: Any] = ["outcome": "different", "candidate": "gpt-6.1-sol", "valid_samples": 4,
            "confirmation": ["initial_mismatch": true, "completed": 3, "status": "confirmed_mismatch"]]
        for phase in ["preparing", "queued", "running", "cancelling"] {
            let fresh = ModelSnapshot(json: ["session_id": "a", "attribution": [
                "status": phase, "confirmation": NSNull(), "task": ["id": "fresh-" + phase, "status": phase],
                "result": oldConfirmed]])
            check(fresh.attribution.confirmation == nil && !fresh.attribution.warning && fresh.referenceValue == "归因中" &&
                  fresh.numericStatus != "confirming", "an active new task never borrows a prior completed confirmation")
        }
        let cancelledResult = ModelSnapshot(json: ["session_id": "a", "manual_available": true, "attribution": ["result": [
            "cancelled": true, "outcome": "inconclusive", "label": "已取消归因", "attempts": 1, "valid_samples": 0,
            "confirmation": ["initial_mismatch": true, "status": "incomplete"]]]])
        check(cancelledResult.numericStatus == "cancelled" && cancelledResult.label == "已取消归因" &&
              cancelledResult.attributionFooter == "已取消归因" && cancelledResult.attribution.recordReason == "用户取消" &&
              !cancelledResult.attribution.warning && cancelledResult.canAttribute,
              "completed cancellation is distinct from failure, clears the current warning, and permits the next task")
        let diagnosticResult: [String: Any] = [
            "outcome": "inconclusive", "label": "参考倾向 gpt-5.6-sol", "candidate": "gpt-5.6-sol",
            "reference_supported": false, "valid_samples": 3, "attempts": 3, "stability": "mixed",
            "sample_candidates": ["gpt-5.6-sol", "gpt-6-sol", "gpt-5.6-sol"],
            "sample_timings": [["attempt": 1, "total_seconds": 83.0, "startup_seconds": 2.0, "metadata_seconds": 1.0, "fork_seconds": 3.0, "model_seconds": 77.0],
                               ["attempt": 2, "total_seconds": 92.0], ["attempt": 3, "total_seconds": 80.0]]
        ]
        let diagnostic = AttributionSnapshot(json: ["result": diagnosticResult])
        check(diagnostic.resultLabel == "参考倾向 gpt-5.6-sol" && diagnostic.sampleTimings.map(\.label) == ["第1份 83s", "第2份 92s", "第3份 80s"],
              "numeric reference and per-sample duration survive an inconclusive outcome")
        check(diagnostic.stabilityLabel == "稳定性：候选分歧" && diagnostic.sampleTimings[0].detail.contains("模型 77s"),
              "stability and stage durations remain diagnostic metadata")
        let independent = ModelSnapshot(json: ["session_id": "normal-chat", "selected_model": "gpt-5.6-sol",
                                               "attribution": ["sample_scope": "new_session", "completed_at": 1_793_168_520.0, "result": [
                                                "outcome": "close", "label": "接近所选模型", "model": "gpt-6.1-sol", "effort": "ultra", "valid_samples": 1]]])
        check(independent.selectedModel == "gpt-5.6-sol" && independent.attribution.model == "gpt-6.1-sol" && independent.attribution.scopeLabel.contains("新会话采样"),
              "independent sampling retains its own configuration and scope without replacing the monitored selection")
        check(independent.referenceValue == "gpt-6.1-sol" && independent.attributionRowLabel == "返回模型" &&
              independent.attributionFooter.hasPrefix("独立检测 · "),
              "independent results remain concise and explicitly separate from the monitored conversation")
        let busyWithDetection = ModelSnapshot(json: ["session_id": "a", "selection_mode": "bound", "attribution": [
            "enabled": true, "completed_at": 1_793_168_520.0,
            "task": ["id": "new-check", "status": "running", "source": "automatic", "progress": "初检中 · 8秒"],
            "auto_progress": ["tools": 19, "threshold": 24, "pending": false],
            "result": ["outcome": "close", "candidate": "gpt-6.1-sol", "valid_samples": 1]]])
        check(busyWithDetection.attributionFooter == "初检中 · 8秒" && busyWithDetection.lastDetectionLine?.hasPrefix("上次检测 · ") == true &&
              busyWithDetection.automaticStatus == "自动监测执行中",
              "active checks keep the prior detection timestamp visible separately from current progress")
        let automaticIdle = ModelSnapshot(json: ["session_id": "a", "selection_mode": "bound", "attribution": ["enabled": true,
            "auto_progress": ["tools": 7, "threshold": 24, "pending": false, "last_completed_at": 1_793_168_520.0]]])
        check(automaticIdle.automaticStatus == "自动监测 · 工具完成 7/24" && automaticIdle.lastDetectionLine?.hasPrefix("上次检测 · ") == true,
              "idle automatic status uses the monitored conversation's real checkpoint count and timestamp")
        let automaticPending = ModelSnapshot(json: ["session_id": "a", "attribution": ["enabled": true,
            "auto_progress": ["tools": 24, "threshold": 24, "pending": true]]])
        check(automaticPending.automaticStatus == "自动监测 · 等待当前任务", "pending automatic work has an explicit waiting status")
        let automaticMissingCounts = ModelSnapshot(json: ["session_id": "a", "attribution": ["enabled": true]])
        check(automaticMissingCounts.automaticStatus == "自动监测已开启" && automaticMissingCounts.lastDetectionLine == nil,
              "missing checkpoint data never fabricates tool counts or a detection time")
        let invalidAutomaticCounts = ModelSnapshot(json: ["session_id": "a", "attribution": ["enabled": true,
            "auto_progress": ["tools": true, "threshold": 20]]])
        check(invalidAutomaticCounts.automaticStatus == "自动监测已开启", "boolean data is never presented as a fabricated tool count")
        let automaticOff = ModelSnapshot(json: ["selection_mode": "bound", "attribution": ["enabled": false]])
        check(automaticOff.automaticStatus == "自动监测已关闭 · 已绑定", "disabled automatic monitoring clearly identifies its bound session state")
        let independentWithoutTime = ModelSnapshot(json: ["session_id": "a", "attribution": ["sample_scope": "new_session", "enabled": true,
            "auto_progress": ["tools": 3, "threshold": 20, "last_completed_at": 1_793_168_520.0]]])
        check(independentWithoutTime.lastDetectionLine == nil && independentWithoutTime.automaticStatus == "自动监测 · 工具完成 3/20",
              "independent results do not borrow the source timestamp while automatic progress still describes the monitored session")
        let automaticWithoutSession = ModelSnapshot(json: ["attribution": ["enabled": true, "auto_available": false]])
        check(automaticWithoutSession.automaticStatus == "自动监测已开启 · 等待会话" && automaticWithoutSession.canSetAutomatic,
              "a missing conversation retains the real global setting and permits disabling it")
        check(automaticWithoutSession.automaticCommand(enabled: false)?["session_id"] is NSNull &&
              automaticWithoutSession.automaticCommand(enabled: false)?["enabled"] as? Bool == false &&
              automaticWithoutSession.automaticCommand(enabled: true) == nil,
              "global disable does not require a target while enabling still requires a usable conversation")
        let automaticReadError = ModelSnapshot(json: ["session_id": "a", "status": "error", "auto_available": false,
                                                       "attribution": ["enabled": true]])
        check(automaticReadError.automaticStatus == "自动监测已开启 · 日志读取失败" && automaticReadError.canSetAutomatic &&
              automaticReadError.automaticCommand(enabled: false)?["session_id"] as? String == "a" &&
              automaticReadError.automaticCommand(enabled: true) == nil,
              "a log read failure reports enabled monitoring and keeps global disable available")
        let readerFailure = ModelSnapshot.failure("读取组件已退出").retainingAutomaticSetting(from: automaticIdle)
        check(readerFailure.attribution.enabled && readerFailure.automaticStatus == "自动监测已开启 · 日志读取失败" &&
              !ModelSnapshot(json: ["status": "error", "attribution": ["enabled": false]])
                .retainingAutomaticSetting(from: automaticIdle).attribution.enabled,
              "local reader failures preserve the last known global switch while explicit backend settings remain authoritative")
        let switchingAutomatic = busyWithDetection.switching(to: "b")
        check(switchingAutomatic.attribution.enabled && switchingAutomatic.sessionID == "b" &&
              switchingAutomatic.automaticStatus == "自动监测已开启 · 正在切换会话" && switchingAutomatic.canSetAutomatic &&
              switchingAutomatic.attribution.result == nil && !switchingAutomatic.attribution.busy && switchingAutomatic.attribution.autoTools == nil,
              "switching keeps the global switch without relabeling prior task, result, or tool progress")
        let runningFooter = AttributionFooterLayout(state: busyWithDetection, width: 336)
        let completedFooter = AttributionFooterLayout(state: previousResult, width: 336)
        check(runningFooter.summary == "初检中 · 8秒" && runningFooter.detection != nil && completedFooter.summary == nil &&
              completedFooter.detection != nil && runningFooter.detectionAlignment == .right && completedFooter.detectionAlignment == .left &&
              completedFooter.detectionRect?.minX == 15 && completedFooter.detectionRect?.width == 306 &&
              completedFooter.detectionRect?.minY == runningFooter.detectionRect?.minY &&
              runningFooter.detectionRect?.maxX == 321 && runningFooter.summaryRect.maxX + 8 == runningFooter.detectionRect?.minX &&
              runningFooter.summaryRect.minY == runningFooter.detectionRect?.minY,
              "detection time sits right beside live progress and left when it is the only footer text, on the same baseline")
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.prohibited)
        let defaultForm = NewSessionSampleForm(state: ModelSnapshot())
        check(defaultForm.modelField.isEditable && defaultForm.modelField.stringValue == "gpt-6.1-sol" &&
              defaultForm.command?["effort"] as? String == "ultra" && defaultForm.providerField.stringValue == "openai",
              "new-session form has editable model and explicit default effort/provider")
        check(defaultForm.subviews.compactMap { ($0 as? NSTextField)?.stringValue }.allSatisfy { !["ultra", "强度", "推理强度"].contains($0) } &&
              !defaultForm.subviews.contains(where: { $0 is NSPopUpButton }),
              "new-session form displays only model and provider while retaining the required protocol parameter privately")
        let chosenForm = NewSessionSampleForm(state: ModelSnapshot(json: ["selected_model": "gpt-6.1-terra", "selected_effort": "high",
                                                                         "selected_provider": "chosen-provider", "configuration_provider": "other-provider"]))
        check(chosenForm.modelField.stringValue == "gpt-6.1-terra" && chosenForm.command?["effort"] as? String == "high" && chosenForm.providerField.stringValue == "chosen-provider",
              "new-session form defaults to explicit local selection before turn configuration")
        chosenForm.modelField.stringValue = " gpt-6.1-sol "
        chosenForm.providerField.stringValue = "openai"
        let newCommand = chosenForm.command!
        check(newCommand["action"] as? String == "sample_new" && newCommand["model"] as? String == "gpt-6.1-sol" && newCommand["provider"] as? String == "openai" && newCommand["effort"] as? String == "high" && newCommand["session_id"] == nil,
              "new-session command contains only explicitly chosen configuration without a monitored conversation ID")
        chosenForm.modelField.stringValue = "invalid model"
        check(chosenForm.command == nil, "new-session form rejects invalid model identifiers before sending")
        let preview = MonitorView(frame: NSRect(x: 0, y: 0, width: 360, height: 320))
        preview.collapsed = false
        preview.state = configurationOnly
        preview.layoutSubtreeIfNeeded()
        for theme in MonitorTheme.allCases {
            preview.theme = theme
            preview.state = confirming
            let confirmingColor = preview.statusColor
            let confirmingSymbol = preview.statusSymbol
            preview.state = different
            check(confirmingColor.isEqual(preview.statusColor) && confirmingSymbol == "!" && preview.statusSymbol == "!",
                  "confirmation warning uses the same amber icon and text as a difference in both themes")
        }
        preview.theme = .graphite
        preview.state = configurationOnly
        func descendants(_ view: NSView) -> [NSView] { view.subviews.flatMap { [$0] + descendants($0) } }
        let buttons = descendants(preview).compactMap { $0 as? NSButton }
        check(!buttons.contains(where: { $0.title.contains("归因") || $0.title == "新会话采样" }),
              "information card has no sampling buttons; attribution remains in the native right-click menu")
        check(preview.toolTip?.contains("实际模型") == false && preview.toolTip?.contains("平台") == false && preview.toolTip?.contains("数字指纹参考") == true,
              "card accessibility and tooltip contain numeric references without retired platform evidence")
        check(preview.toolTip?.contains("high") == false && preview.toolTip?.contains("ultra") == false && preview.toolTip?.contains("强度") == false,
              "card tooltip does not expose hidden native effort settings")
        let testWindow = NSPanel(contentRect: preview.frame, styleMask: .borderless, backing: .buffered, defer: false)
        testWindow.isReleasedWhenClosed = false
        testWindow.contentView = preview
        var opened = 0, sent = 0, closed = 0, menuOpenings = 0
        preview.onKeepOpen = { opened += 1; preview.collapsed = false }
        preview.onToggle = { closed += 1; preview.collapsed = true }
        preview.onMenuChanged = { if $0 { menuOpenings += 1 } }
        preview.onAttribute = { sent += 1; return true }
        func mouse(_ type: NSEvent.EventType, _ clicks: Int, _ point: NSPoint = NSPoint(x: 150, y: 150)) -> NSEvent {
            NSEvent.mouseEvent(with: type, location: preview.convert(point, to: nil), modifierFlags: [],
                               timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: testWindow.windowNumber,
                               context: nil, eventNumber: 1, clickCount: clicks, pressure: 0)!
        }
        preview.collapsed = true
        preview.mouseDown(with: mouse(.leftMouseDown, 1)); preview.mouseUp(with: mouse(.leftMouseUp, 1))
        check(!preview.collapsed && opened == 1 && closed == 0 && sent == 0,
              "single orb click immediately opens and fixes the information card")
        preview.mouseDown(with: mouse(.leftMouseDown, 1)); preview.mouseUp(with: mouse(.leftMouseUp, 1))
        check(!preview.collapsed && closed == 0, "blank card click never closes the information card")
        _ = preview.accessibilityPerformPress()
        check(!preview.collapsed && closed == 0, "accessibility press keeps an expanded information card open")
        preview.mouseDown(with: mouse(.leftMouseDown, 2)); preview.mouseUp(with: mouse(.leftMouseUp, 2))
        check(menuOpenings == 0 && sent == 0 && !preview.collapsed, "double click has no menu or sampling action")
        let openedBeforeDrag = opened
        preview.mouseDown(with: mouse(.leftMouseDown, 1))
        preview.mouseDragged(with: mouse(.leftMouseDragged, 1, NSPoint(x: 180, y: 180)))
        preview.mouseUp(with: mouse(.leftMouseUp, 1, NSPoint(x: 180, y: 180)))
        check(opened == openedBeforeDrag && closed == 0, "drag never triggers a click action")
        let minus = NSPoint(x: preview.bounds.width - 40, y: 36)
        preview.mouseDown(with: mouse(.leftMouseDown, 1, minus)); preview.mouseUp(with: mouse(.leftMouseUp, 1, minus))
        check(preview.collapsed && closed == 1, "only the explicit minus control collapses the information card")
        for type in [NSEvent.EventType.mouseEntered, .mouseExited] {
            let event = NSEvent.enterExitEvent(with: type, location: .zero, modifierFlags: [], timestamp: 1,
                                               windowNumber: testWindow.windowNumber, context: nil,
                                               eventNumber: 1, trackingNumber: 0, userData: nil)!
            if type == .mouseEntered { preview.mouseEntered(with: event) } else { preview.mouseExited(with: event) }
        }
        RunLoop.current.run(until: Date().addingTimeInterval(0.30))
        check(preview.collapsed && opened == openedBeforeDrag && closed == 1,
              "pointer entry and exit never change expansion state")
        preview.collapsed = false
        let menu = preview.makeOperationsMenu()
        check(menu.minimumWidth >= 220 && menu.items[0].title == "手动数字归因" && menu.items[1].title == "自动数字归因",
              "native menu has independent width and the two primary attribution actions")
        check(menu.items[1].state == .off, "automatic attribution is unchecked when disabled")
        check(menu.items.map(\.title).contains("主题") && menu.items.map(\.title).contains("监测会话选择") && !menu.items.map(\.title).contains("选项"),
              "theme and conversation selection are first-level native submenus")
        check(menu.items.first(where: { $0.title == "主题" })?.submenu?.items.first?.state == .on,
              "selected theme uses the native menu checkmark")
        check(!((menu.items.first(where: { $0.title == "监测会话选择" })?.submenu?.items.map(\.title) ?? []).contains("绑定当前显示会话")),
              "conversation menu removes the obsolete bind-current entry")
        var toggles: [Bool] = []
        preview.onSetAuto = { toggles.append($0) }
        _ = NSApp.sendAction(menu.items[1].action!, to: menu.items[1].target, from: menu.items[1])
        check(toggles == [true] && sent == 0, "clicking the automatic item toggles its setting without directly sampling")
        check(preview.makeOperationsMenu().items[1].state == .on, "the next menu immediately reflects the local toggle before a backend poll")
        var automaticState = configurationOnly
        automaticState.attribution.enabled = true
        preview.state = automaticState
        let enabledMenu = preview.makeOperationsMenu()
        check(enabledMenu.items[1].state == .on, "reopening the native menu shows a real checkmark for the saved automatic setting")
        _ = NSApp.sendAction(enabledMenu.items[0].action!, to: enabledMenu.items[0].target, from: enabledMenu.items[0])
        check(sent == 1, "the explicit native manual item performs one attribution")
        check(preview.preferredExpandedHeight == 320, "opening a native menu never stretches the information card")
        preview.state = ModelSnapshot(json: ["selection_mode": "recent_activity"])
        let unavailable = preview.makeOperationsMenu()
        check(!unavailable.items[0].isEnabled && !unavailable.items[1].isEnabled,
              "a missing session ID disables existing-conversation actions")
        var globalDisables: [Bool] = []
        preview.onSetAuto = { globalDisables.append($0) }
        preview.state = automaticWithoutSession
        let noSessionAutomatic = preview.makeOperationsMenu().items[1]
        check(noSessionAutomatic.isEnabled && noSessionAutomatic.state == .on,
              "the checked global switch remains actionable while waiting for a conversation")
        _ = NSApp.sendAction(noSessionAutomatic.action!, to: noSessionAutomatic.target, from: noSessionAutomatic)
        check(globalDisables == [false] && !preview.state.attribution.enabled,
              "the no-session menu explicitly disables the global setting")
        preview.state = automaticReadError
        let failedReadAutomatic = preview.makeOperationsMenu().items[1]
        check(failedReadAutomatic.isEnabled && failedReadAutomatic.state == .on,
              "log errors do not disable the checked global automatic control")
        _ = NSApp.sendAction(failedReadAutomatic.action!, to: failedReadAutomatic.target, from: failedReadAutomatic)
        check(globalDisables == [false, false], "the read-error menu still sends a global disable")
        preview.state = ModelSnapshot(json: ["session_id": "b", "attribution": ["enabled": true]])
        _ = NSApp.sendAction(noSessionAutomatic.action!, to: noSessionAutomatic.target, from: noSessionAutomatic)
        check(globalDisables == [false, false], "a stale no-session menu cannot toggle after a conversation binding changes")
        preview.onSetAuto = { toggles.append($0) }
        preview.state = ModelSnapshot(json: ["selection_mode": "recent_activity"])
        var newSessionChoices = 0
        preview.onNewSessionSample = { newSessionChoices += 1 }
        let newSessionItem = preview.makeOperationsMenu().items.first { $0.title == "新会话采样…" }!
        _ = NSApp.sendAction(newSessionItem.action!, to: newSessionItem.target, from: newSessionItem)
        check(newSessionChoices == 1 && sent == 1, "new-session menu only opens its explicit configuration callback")
        preview.state = ModelSnapshot(json: ["manual_available": true, "requires_new_sample": true])
        check(preview.state.canAttribute, "an empty target can explicitly open new-session sampling when the controller permits it")
        let emptyTargetManual = preview.makeOperationsMenu().items[0]
        _ = NSApp.sendAction(emptyTargetManual.action!, to: emptyTargetManual.target, from: emptyTargetManual)
        check(newSessionChoices == 2 && sent == 1, "empty-target manual action opens configuration instead of inventing a conversation target")
        preview.state = ModelSnapshot(json: ["attribution": ["sample_scope": "new_session", "task": [
            "id": "standalone-A", "session_id": "new-sample-A", "status": "running", "cancellable": true]]])
        let oldCancel = preview.makeOperationsMenu().items.first { $0.title == "取消本次归因" }!
        let cancellation = preview.state.cancelAttributionCommand
        check(cancellation["task_id"] as? String == "standalone-A" && cancellation["session_id"] is NSNull,
              "standalone cancellation carries the task ID even without a monitored session ID")
        var cancelled = 0
        preview.onCancelAttribution = { cancelled += 1 }
        preview.state = ModelSnapshot(json: ["attribution": ["sample_scope": "new_session", "task": [
            "id": "standalone-B", "session_id": "new-sample-B", "status": "running", "cancellable": true]]])
        _ = NSApp.sendAction(oldCancel.action!, to: oldCancel.target, from: oldCancel)
        check(cancelled == 0, "an old cancellation menu cannot cancel a newer standalone task")
        var changedSession = configurationOnly
        changedSession.sessionID = "b"
        preview.state = changedSession
        _ = NSApp.sendAction(enabledMenu.items[0].action!, to: enabledMenu.items[0].target, from: enabledMenu.items[0])
        _ = NSApp.sendAction(enabledMenu.items[1].action!, to: enabledMenu.items[1].target, from: enabledMenu.items[1])
        check(sent == 1 && toggles == [true], "an old menu from A never samples or changes the automatic switch of B")
        preview.state = configurationOnly
        preview.onAttribute = { false }
        let previewRejected = preview.makeOperationsMenu().items[0]
        _ = NSApp.sendAction(previewRejected.action!, to: previewRejected.target, from: previewRejected)
        check(preview.makeOperationsMenu().items[0].isEnabled && !buttons.contains(where: { $0.title.contains("归因") }),
              "preview rejection clears the native manual menu pending state without recreating a card action")
        preview.state = ModelSnapshot(json: ["session_id": "a", "manual_available": true,
            "attribution": ["result": oldConfirmed]])
        preview.markMenuSubmissionPending()
        check(preview.state.attribution.confirmation == nil && preview.state.attribution.currentCandidate == nil &&
              preview.state.numericStatus == "running" && preview.state.referenceValue == "归因中" && !preview.state.attribution.warning,
              "optimistic submission immediately clears the old confirmation and candidate")
        preview.clearMenuSubmissionPending()
        check(preview.state.numericStatus == "different" && preview.state.canAttribute,
              "a rejected submission restores the previous completed result and manual eligibility")
        preview.state = configurationOnly
        var acceptedFromReadyState = false
        preview.onAttribute = {
            acceptedFromReadyState = preview.state.canAttribute
            if acceptedFromReadyState { preview.markMenuSubmissionPending() }
            return acceptedFromReadyState
        }
        let acknowledgedManual = preview.makeOperationsMenu().items[0]
        _ = NSApp.sendAction(acknowledgedManual.action!, to: acknowledgedManual.target, from: acknowledgedManual)
        check(acceptedFromReadyState && preview.state.attribution.busy,
              "the manual callback validates the ready state before optimistic busy state is applied")
        preview.state = ModelSnapshot(json: ["session_id": "a", "attribution": ["task": [
            "id": "cancel-now", "status": "running", "cancellable": true]]])
        var cancelRequested = 0
        preview.onCancelAttribution = { cancelRequested += 1 }
        let cancelNow = preview.makeOperationsMenu().items.first { $0.title == "取消本次归因" }!
        _ = NSApp.sendAction(cancelNow.action!, to: cancelNow.target, from: cancelNow)
        let cancellingItem = preview.makeOperationsMenu().items.first { $0.title == "正在取消…" }
        check(cancelRequested == 1 && preview.state.numericStatus == "cancelling" &&
              cancellingItem?.isEnabled == false && !preview.state.canAttribute,
              "cancel click immediately reports cancelling and prevents repeated cancellation or new submissions")
        preview.state = ModelSnapshot(json: ["session_id": "a", "attribution": ["task": [
            "id": "first-sample", "status": "running", "progress": "采样 1/4 · 有效 0"]]])
        let firstSampleRecords = preview.makeOperationsMenu().items.first { $0.title == "查看归因记录" }!.submenu!.items.map(\.title)
        check(firstSampleRecords.contains(where: { $0.hasPrefix("进行中：") }) && !firstSampleRecords.contains("尚无数字归因记录"),
              "a first running sample shows progress without a contradictory empty-history row")
        preview.state = ModelSnapshot(json: ["session_id": "a", "attribution": ["status": "idle"]])
        let idleRecords = preview.makeOperationsMenu().items.first { $0.title == "查看归因记录" }!.submenu!.items.map(\.title)
        check(idleRecords.contains("尚无数字归因记录") && !idleRecords.contains(where: { $0.hasPrefix("进行中：") }),
              "idle unsampled conversations retain the empty-history row")
        preview.state = ModelSnapshot(json: ["session_id": "a", "attribution": ["scope": "previous_turn",
            "task": ["id": "next-sample", "status": "running", "progress": "采样 1/4 · 有效 0"],
            "result": ["outcome": "close", "model": "gpt-6-sol", "candidate": "gpt-6-sol", "valid_samples": 1]]])
        let runningHistory = preview.makeOperationsMenu().items.first { $0.title == "查看归因记录" }!.submenu!.items.map(\.title)
        check(runningHistory.contains(where: { $0.hasPrefix("进行中：") }) && runningHistory.contains("上次归因记录") &&
              runningHistory.contains("返回模型：gpt-6-sol") && !runningHistory.contains("尚无数字归因记录"),
              "a running task keeps concise prior result fields alongside current progress")
        preview.state = ModelSnapshot(json: ["session_id": "a", "attribution": ["sample_scope": "new_session",
            "task": ["id": "next-independent-sample", "status": "running", "progress": "初检中"],
            "result": ["outcome": "close", "model": "gpt-6.1-sol", "candidate": "gpt-6.1-sol", "valid_samples": 1]]])
        let independentRunningRecords = preview.makeOperationsMenu().items.first { $0.title == "查看归因记录" }!.submenu!.items.map(\.title)
        check(independentRunningRecords.count == 8 && independentRunningRecords[1] == "上次新会话归因记录" &&
              independentRunningRecords.contains("有效样本：1"),
              "running independent sampling clearly labels the seven completed-result rows as the previous result")
        preview.state = ModelSnapshot(json: ["session_id": "a", "selected_model": "gpt-6.1-sol", "selected_effort": "ultra",
            "attribution": ["result": diagnosticResult]])
        let diagnosticsMenu = preview.makeOperationsMenu().items.first { $0.title == "查看归因记录" }!.submenu!
        check(diagnosticsMenu.items.map(\.title) == ["数字归因记录", "时间：未记录", "有效样本：3", "尝试次数：3",
            "当前配置模型：gpt-6.1-sol", "返回模型：gpt-5.6-sol", "原因：归因结果待复测"],
              "records show exactly seven requested fields with the currently displayed configuration")
        check(diagnosticsMenu.items.allSatisfy { $0.toolTip == nil }, "records do not retain redundant diagnostic tooltips")
        preview.state = independent
        let independentRecords = preview.makeOperationsMenu().items.first { $0.title == "查看归因记录" }!.submenu!.items.map(\.title)
        check(independentRecords.count == 7 && independentRecords.first == "新会话归因记录" &&
              independentRecords.contains("当前配置模型：gpt-6.1-sol") && independentRecords.contains("返回模型：gpt-6.1-sol") &&
              independentRecords.contains("原因：匹配所选型号"),
              "standalone records use the sampled configuration without borrowing the monitored conversation model")
        preview.state = preparationFailure
        let preparationRecords = preview.makeOperationsMenu().items.first { $0.title == "查看归因记录" }!.submenu!.items.map(\.title)
        check(preparationRecords.count == 7 && preparationRecords.contains("尝试次数：0") &&
              preparationRecords.contains("返回模型：未返回") && preparationRecords.contains("原因：快照准备失败"),
              "preparation failures keep an accurate short reason and never fabricate a returned model")
        check(confirmed.attribution.recordReason == "三次复测均不一致" && notConfirmed.attribution.recordReason == "复测未持续异常",
              "confirmation reasons remain short Chinese descriptions")
        for model in ["gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol"] {
            let same = ModelSnapshot(json: ["selected_model": model, "attribution": ["result": [
                "outcome": "close", "candidate": model, "model": model, "valid_samples": 1]]])
            check(same.attributionRowLabel == "返回模型" && same.referenceValue == model && same.label == "归因匹配",
                  "all supported model versions share the same returned-model presentation")
        }
        print("\(count) snapshot assertions passed")
    }
}
