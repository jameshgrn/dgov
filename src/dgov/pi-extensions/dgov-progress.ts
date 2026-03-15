import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

const SLUG = process.env.DGOV_SLUG;
const SESSION_ROOT = process.env.DGOV_SESSION_ROOT;

function enabled(): boolean {
  return !!(SLUG && SESSION_ROOT);
}

function progressPath(): string {
  return `${SESSION_ROOT}/.dgov/progress/${SLUG}.json`;
}

let turnCount = 0;
let lastTool = "";
let lastMessage = "";

async function writeProgress(
  pi: ExtensionAPI,
  status: "working" | "done" | "exited"
): Promise<void> {
  const dir = `${SESSION_ROOT}/.dgov/progress`;
  const payload = JSON.stringify({
    slug: SLUG,
    turn: turnCount,
    timestamp: new Date().toISOString(),
    last_tool: lastTool,
    status,
    message: lastMessage.slice(0, 100),
  });
  // Escape single quotes in JSON for shell safety
  const escaped = payload.replace(/'/g, "'\\''");
  await pi.exec("mkdir", ["-p", dir]);
  await pi.exec("sh", ["-c", `printf '%s' '${escaped}' > '${progressPath()}'`]);
}

export default function (pi: ExtensionAPI) {
  if (!enabled()) return;

  pi.on("turn_start", async (_event, ctx) => {
    turnCount++;

    // Extract last assistant message snippet from conversation history
    const entries = ctx.sessionManager.getEntries();
    for (let i = entries.length - 1; i >= 0; i--) {
      const entry = entries[i];
      if (entry.role === "assistant" && typeof entry.content === "string") {
        lastMessage = entry.content.trim();
        break;
      }
    }

    await writeProgress(pi, "working");
  });

  pi.on("tool_result", async (event, _ctx) => {
    if (event.tool) {
      lastTool = event.tool;
    }
  });

  pi.on("agent_end", async (_event, _ctx) => {
    await writeProgress(pi, "done");
  });

  pi.on("session_shutdown", async (_event, _ctx) => {
    await writeProgress(pi, "exited");
  });
}
