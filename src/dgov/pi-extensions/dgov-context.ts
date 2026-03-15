import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

export default function(pi: ExtensionAPI) {
  let firstTurn = true;

  pi.on("turn_start", async (_event, _ctx) => {
    if (!firstTurn) return;
    firstTurn = false;

    const envVars = [
      "DGOV_SLUG",
      "DGOV_AGENT",
      "DGOV_BRANCH",
      "DGOV_BASE_SHA",
      "DGOV_ROOT",
      "DGOV_SESSION_ROOT",
      "DGOV_WORKTREE_PATH",
    ];

    const dgovEnv: Record<string, string | undefined> = {};
    let hasAny = false;

    for (const key of envVars) {
      const val = process.env[key];
      if (val) {
        dgovEnv[key] = val;
        hasAny = true;
      }
    }

    // Skip silently if no dgov env vars are found
    if (!hasAny) return;

    let workerClaudeMd = "";
    try {
      // Use pi.exec to cat the CLAUDE.md in the worktree root
      const { stdout, code } = await pi.exec("cat", ["CLAUDE.md"]);
      if (code === 0) {
        workerClaudeMd = stdout;
      }
    } catch (e) {
      // Fail silently if CLAUDE.md is not found or readable
    }

    const summary = `You are a dgov worker. Slug: ${dgovEnv.DGOV_SLUG}, Branch: ${dgovEnv.DGOV_BRANCH}, Agent: ${dgovEnv.DGOV_AGENT}. Your task is defined in the prompt. Commit your work when done.`;

    const contextContent = `
# dgov Worker Context
${summary}

**Metadata:**
- Base SHA: ${dgovEnv.DGOV_BASE_SHA ?? "unknown"}
- Root: ${dgovEnv.DGOV_ROOT ?? "unknown"}
- Session Root: ${dgovEnv.DGOV_SESSION_ROOT ?? "unknown"}
- Worktree Path: ${dgovEnv.DGOV_WORKTREE_PATH ?? "unknown"}

${workerClaudeMd ? `\n## Worker Instructions (CLAUDE.md)\n${workerClaudeMd}` : ""}
`.trim();

    if (typeof (pi as any).appendSystemPrompt === "function") {
      (pi as any).appendSystemPrompt(contextContent);
    } else {
      // Fallback if appendSystemPrompt is not available
      await pi.exec("echo", ["[dgov] Context injected: " + summary]);
    }
  });
}
