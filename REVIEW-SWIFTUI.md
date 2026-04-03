# SwiftUI Review: RiverDrop

Reviewer: Claude (senior SwiftUI / Apple HIG)
Date: 2026-03-04
Scope: MainView, ConnectionView, LocalBrowserView, PaywallView, DryRunPreviewView, DropZoneView, DesignSystem, SettingsView, RiverDropApp

---

## 1. Navigation & State

### What works

- **@StateObject ownership is correct.** `RiverDropApp.swift:5-7` owns all three service objects (`SFTPService`, `TransferManager`, `StoreManager`) as `@StateObject`, injected via `.environmentObject()` at lines 30-32. All child views use `@EnvironmentObject`. No ownership inversion.
- **HSplitView is the right choice** for a dual-pane file manager on macOS (`MainView.swift:186`).
- **Conditional root view** at `RiverDropApp.swift:24-28` (connected vs. not) is simple and appropriate for a two-state app. NavigationStack/NavigationSplitView is unnecessary here.

### Issues

**M-1: Duplicate `stagedUploads` state across views**
`MainView.swift:159` and `LocalBrowserView.swift:38` each declare their own `@State private var stagedUploads`. Both have an `uploadStaged()` method (`LocalBrowserView.swift:759`, `MainView.swift:1231`). Files dropped on the local pane stage into LocalBrowserView's array; files dropped on the remote pane stage into MainView's array. A user who stages files from both sides will see items in two separate lists with no indication they're disjoint.

Suggestion: Unify staging into a single source of truth. Either pass `$stagedUploads` from MainView to LocalBrowserView as a binding, or hoist staging into `TransferManager`.

**M-2: NotificationCenter for in-app navigation**
`RiverDropApp.swift:81-106` posts `NotificationCenter` notifications for the Go menu, consumed at `MainView.swift:225-228`. This bypasses SwiftUI's data flow. If a second window is opened (WindowGroup allows it), every window will receive every notification.

Suggestion: Use a FocusedValue or an @Environment key so menu commands target the active window's state.

**L-1: No NavigationStack for future deep-linking**
Not a problem today, but if RiverDrop ever needs programmatic back-navigation, history, or deep links (e.g., opening a URL scheme to a specific remote path), the flat conditional will need to be replaced. Worth noting for roadmap.

---

## 2. Async UX

### What works

- Connection shows a spinner and "Connecting..." (`ConnectionView.swift:310-318`).
- Transfers show progress bars with percentage (`MainView.swift:1058-1080`).
- Transfers are cancellable mid-flight (`MainView.swift:1082-1090`).
- DryRun shows a spinner while running (`MainView.swift:417-419`) and an applying state (`DryRunPreviewView.swift:168-175`).
- Error messages throughout include "Suggested fix:" — above average for actionable errors.

### Issues

**H-1: No cancel button for connection attempt**
`ConnectionView.swift:331-364` starts an async Task but provides no way to cancel it. If the SSH server is slow or unreachable, the user is stuck watching a spinner with no escape. The form is disabled (`line 39`) so they can't even clear the host field.

Suggestion: Store the Task handle, add a "Cancel" button next to the ProgressView, and cancel the task on tap. Clear `isConnecting` on cancellation.

**H-2: No loading indicator for remote directory listing**
When navigating to a remote folder (`MainView.swift:738-744`), the file list briefly shows "Empty directory" before files load. There's no spinner or skeleton state. This is jarring, especially over high-latency connections.

Suggestion: Add an `isLoadingDirectory` state to SFTPService (or check an existing one) and show a centered ProgressView in the remote file list while loading.

**M-3: No loading indicator for Refresh**
Clicking the refresh button (`MainView.swift:361-363`) calls `sftpService.listDirectory()` with no visual feedback. The list silently updates. User can't tell if the refresh started or completed.

Suggestion: Show a brief spinner overlay or swap the refresh icon to a spinning animation while the async call is in flight.

**M-4: No retry for failed transfers**
`MainView.swift:1151-1157` shows a red "Failed" badge but offers no retry action. The user must re-initiate the transfer manually (find the file, select it, download again).

Suggestion: Add a retry button next to failed transfers in the transfer log. Store enough context (remote path, local dir, direction) to re-enqueue.

**L-2: No connection loss detection**
If the SSH connection drops mid-session, there's no automatic detection or reconnection UI. The app stays on MainView with a stale state until the next operation fails.

Suggestion: Implement a heartbeat or catch errors from `sftpService.listDirectory()` / transfer operations and transition back to ConnectionView (or show a reconnection banner) when the connection is lost.

---

## 3. Accessibility

### Issues

**H-3: Hardcoded font sizes block Dynamic Type scaling**
Nearly all text uses `.font(.system(size: N))` with explicit point sizes:
- `ConnectionView.swift:98` — `.font(.system(size: 42))`
- `ConnectionView.swift:114` — `.font(.system(size: 26))`
- `MainView.swift:750` — `.font(.system(size: 13))`
- `DesignSystem.swift:286` — `.font(.system(size: 14))`
- `DropZoneView.swift:109` — `.font(.system(size: 10))`

None of these scale with the user's text size preference. On macOS 14+, Dynamic Type is supported in System Settings > Accessibility > Display > Text Size.

Suggestion: Replace fixed sizes with semantic styles (`.body`, `.caption`, `.headline`, `.title2`, etc.) or use `.font(.system(.body, design: .rounded))` to preserve design intent while allowing scaling. For icon-adjacent text where precise sizing matters, at minimum use `@ScaledMetric` for the size value.

**H-4: Icon-only buttons lack accessibilityLabel**
Many toolbar buttons use only SF Symbols:
- Go up button: `MainView.swift:351-356` — has `.help("Go up")` but no `.accessibilityLabel`
- Refresh button: `MainView.swift:360-366` — same issue
- Remove staged item: `DropZoneView.swift:174-185` — xmark button has no label at all
- Transfer log chevron: `MainView.swift:982-990` — no label

`.help()` sets the tooltip but is not guaranteed to be read by VoiceOver in all contexts. `.accessibilityLabel` is the reliable mechanism.

Suggestion: Add `.accessibilityLabel("Go to parent directory")`, `.accessibilityLabel("Refresh file list")`, `.accessibilityLabel("Remove \(item.filename) from staging")`, etc.

**M-5: Small click targets**
Several interactive elements are below the HIG recommended 44pt minimum (macOS is more lenient but targets under 20pt are still problematic):
- Staged item dismiss button: `DropZoneView.swift:183` — 14x14pt
- Transfer log disclosure chevron: `MainView.swift:988` — 14pt wide
- Breadcrumb segments: `DesignSystem.swift:235-236` — 6pt horizontal + 3pt vertical padding on 12pt text

Suggestion: Increase frame sizes or add `.contentShape()` with larger hit areas. For the dismiss button, at least 22x22pt with a `.contentShape(Circle())` of 28pt.

**M-6: File rows lack combined accessibility elements**
Remote file rows (`MainView.swift:771-823`) use `.contentShape(Rectangle()).onTapGesture` but don't declare `.accessibilityElement(children: .combine)`. VoiceOver will read each child element (icon, filename, size, date) as separate items rather than a single "filename, 2.4 MB, modified March 3" announcement.

Suggestion: Add `.accessibilityElement(children: .combine)` to file row containers, or manually compose a label like `.accessibilityLabel("\(file.filename), \(formattedSize), modified \(formattedDate)")`.

**L-3: Color-only recently-downloaded indicator**
`LocalBrowserView.swift:624-637` shows a green left bar for newly downloaded files. The "New" text badge at line 646 helps, but the green bar alone (without the badge) relies solely on color.

---

## 4. Design Consistency

### What works

- `RD.Spacing.*` tokens are used broadly and consistently.
- `RD.cornerRadius*` constants are applied to most rounded elements.
- `CardModifier`, `PaneHeader`, `SectionHeader`, `StatusBadge`, `EmptyStateView` provide a coherent component library.
- Brand colors (`riverPrimary`, `riverAccent`, `riverGlow`) are used consistently for accents.
- `RDButtonStyle` is used for all primary/secondary buttons.

### Issues

**M-7: Hardcoded spacing values bypass design tokens**
Multiple locations use raw numbers instead of `RD.Spacing.*`:
- `DropZoneView.swift:126` — `spacing: 4` (should be `RD.Spacing.xs`)
- `DropZoneView.swift:187-189` — `.padding(.leading, 6)`, `.padding(.trailing, 4)`
- `MainView.swift:400-401` — `.padding(.horizontal, 6)`, `.padding(.vertical, 3)`
- `DesignSystem.swift:224` — `spacing: 2` in BreadcrumbView
- `DesignSystem.swift:235-236` — `.padding(.horizontal, 6)`, `.padding(.vertical, 3)`
- `StatusBadge` at `DesignSystem.swift:181-183` — `.padding(.horizontal, 6)`, `.padding(.vertical, 2)`
- `PaywallView.swift:105` — `spacing: 3`

Suggestion: If 2pt, 3pt, and 6pt are intentional sub-token values, define them (e.g., `RD.Spacing.xxs = 2`, `RD.Spacing.micro = 3`). Otherwise, round to the nearest token.

**M-8: Arithmetic on tokens**
`MainView.swift:473`, `527`, `1021` use patterns like `RD.Spacing.xs + 2` and `RD.Spacing.xs + 1`. This creates ad-hoc spacing values that drift from the system.

Suggestion: Define the needed values as new tokens rather than adding to existing ones.

**L-4: Rogue corner radius**
`DesignSystem.swift:241` — BreadcrumbView uses `cornerRadius: 4`, which doesn't match any `RD.cornerRadius*` constant (6, 10, 16).

Suggestion: Use `RD.cornerRadiusSmall` (6) or define `RD.cornerRadiusMicro = 4` if a smaller value is needed.

**L-5: SettingsView uses system Form styling**
`SettingsView.swift` uses default `.padding()` and standard macOS Form styling, not the DesignSystem. This is actually appropriate for a Settings window (per HIG, Settings should feel native), so this is fine. Noting it for completeness.

---

## 5. Edge Cases

### What works

- Empty states are comprehensive: empty directory, no search matches, everything-in-sync dry run, and empty staging areas all have explicit views.
- Minimum window size is enforced at `RiverDropApp.swift:33` (`minWidth: 800, minHeight: 550`).
- Error recovery after failed connection is smooth: error card shown, form stays active for retry, error cleared on next attempt.
- Security-scoped bookmark handling is thorough with stale-bookmark refresh at `LocalBrowserView.swift:857-883`.

### Issues

**H-5: Toolbar overflow at minimum pane width**
`MainView.swift:193-196` sets local pane `minWidth: 250` and remote pane `minWidth: 350`. At 800pt total window width, that leaves ~200pt for one pane after the other takes its minimum. The toolbars pack 7-8 controls into horizontal HStacks without any wrapping or overflow handling. At 250pt, the filter field, buttons, and status badge will truncate or overlap.

Suggestion: Test at minimum width and either increase `minWidth` to accommodate toolbar content, or use a `.toolbar` with overflow behavior, or collapse some buttons into the overflow menu at narrow widths.

**M-9: PaywallView and SettingsView have fixed dimensions**
`PaywallView.swift:37-38` — fixed width 420pt, ideal height 520pt.
`SettingsView.swift:16` — fixed 420x260pt.

If the user has larger accessibility text sizes, content may clip. PaywallView has a ScrollView so vertical overflow is handled, but horizontal overflow with wider text is not.

Suggestion: Use `minWidth`/`idealWidth` instead of fixed `width`, or test with the largest Dynamic Type setting.

**M-10: navigateLocalToCommandPath doesn't load directory**
`MainView.swift:712-725` — When handling the "Navigate to Folder" command with a path string (not panel), it sets `localCurrentDirectory` but doesn't call the full `navigateTo()` method that also clears selection, resets search, and loads the directory. The `LocalBrowserView` depends on `onChange(of: ...)` for some of these but not all.

Suggestion: Call a unified navigation method that resets state and triggers directory load, similar to `LocalBrowserView.navigateTo()`.

**L-6: No retry path for security-scoped bookmark failures**
`LocalBrowserView.swift:876-878` shows an error message when a bookmark can't start security-scoped access, but doesn't automatically offer to re-prompt the user via NSOpenPanel.

**L-7: No handling for very large transfer counts**
The transfer log (`MainView.swift:1023-1035`) uses a ScrollView with `maxHeight: 150` but no virtualization for the transfer list. With hundreds of completed transfers, this list could accumulate unbounded (only manually cleared via trash button).

---

## 6. Pro Upgrade UX

### What works

- Pro features (dry-run, content search) are visible in the toolbar even for free users, creating natural discovery moments.
- Paywall is presented as a sheet (not a blocking modal), so users can dismiss it easily.
- Feature cards in PaywallView clearly explain what Pro offers.
- "Restore Purchases" button is present.
- Product loading state shown when StoreKit hasn't loaded yet.
- Bookmark limit (5) is a reasonable soft gate.

### Issues

**H-6: PaywallView doesn't auto-dismiss after successful purchase**
`PaywallView.swift:147-157` calls `storeManager.purchase()` but never observes the result. After a successful purchase, `storeManager.isPro` presumably becomes true, but the PaywallView stays open. The user must manually click "Not Now" to close the sheet — an anticlimactic post-purchase experience.

Suggestion: Observe `storeManager.isPro` and auto-dismiss with a brief success animation:
```swift
.onChange(of: storeManager.isPro) { _, isPro in
    if isPro {
        // Optional: show brief "Welcome to Pro!" then dismiss
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
            dismiss()
        }
    }
}
```

**M-11: No indication of bookmark limit before hitting it**
`LocalBrowserView.swift:720-723` gates at 5 bookmarks, but the UI doesn't show the user their current count or the limit. They discover the gate only when they try to save a 6th bookmark.

Suggestion: Show "3/5 bookmarks" in the bookmark menu footer for free users, so the gate feels expected rather than surprising.

**M-12: Pro feature buttons don't hint at Pro status**
The dry-run (eye icon) and content search buttons look like normal buttons. A free user clicking them gets the paywall, but there's no visual cue beforehand that these are premium features.

Suggestion: Add a subtle "PRO" badge, a small lock overlay on the icon, or use `.foregroundStyle(.secondary)` with a lock icon to hint that these features are gated. Example: change the `eye` icon to `eye.badge.clock.fill` or overlay a small lock.

**L-8: "Not Now" button uses `.tertiary` styling**
`PaywallView.swift:173-178` — The dismiss button uses `.foregroundStyle(.tertiary)`, making it nearly invisible in both light and dark mode. While this is intentional (encourage purchase), it borders on a dark pattern per Apple's App Review guidelines.

Suggestion: Use `.secondary` styling for "Not Now" to keep it subtle but clearly visible.

---

## Summary Table

| ID | Severity | Category | Description |
|----|----------|----------|-------------|
| H-1 | High | Async UX | No cancel for connection attempts |
| H-2 | High | Async UX | No loading indicator for remote directory listing |
| H-3 | High | Accessibility | All font sizes hardcoded, no Dynamic Type support |
| H-4 | High | Accessibility | Icon-only buttons missing accessibilityLabel |
| H-5 | High | Edge Cases | Toolbar overflow at minimum pane width |
| H-6 | High | Pro UX | PaywallView doesn't auto-dismiss after purchase |
| M-1 | Medium | State | Duplicate stagedUploads in MainView + LocalBrowserView |
| M-2 | Medium | State | NotificationCenter for navigation (multi-window unsafe) |
| M-3 | Medium | Async UX | No loading indicator for Refresh action |
| M-4 | Medium | Async UX | No retry button for failed transfers |
| M-5 | Medium | Accessibility | Click targets under 20pt |
| M-6 | Medium | Accessibility | File rows not combined for VoiceOver |
| M-7 | Medium | Design | Hardcoded spacing values bypass tokens |
| M-8 | Medium | Design | Token arithmetic (e.g., `xs + 2`) |
| M-9 | Medium | Edge Cases | Fixed-size sheets may clip with large text |
| M-10 | Medium | Edge Cases | navigateLocalToCommandPath skips state reset |
| M-11 | Medium | Pro UX | No bookmark limit indicator before gate |
| M-12 | Medium | Pro UX | Pro feature buttons don't hint at Pro requirement |
| L-1 | Low | State | No NavigationStack for future deep-linking |
| L-2 | Low | Async UX | No connection loss detection |
| L-3 | Low | Accessibility | Color-only download indicator (partially mitigated by badge) |
| L-4 | Low | Design | Rogue corner radius 4 in BreadcrumbView |
| L-5 | Low | Design | SettingsView uses system styling (intentional, OK) |
| L-6 | Low | Edge Cases | No auto re-prompt for bookmark failures |
| L-7 | Low | Edge Cases | Transfer log can grow unbounded |
| L-8 | Low | Pro UX | "Not Now" near-invisible with .tertiary |

### Counts: 6 High, 12 Medium, 8 Low
