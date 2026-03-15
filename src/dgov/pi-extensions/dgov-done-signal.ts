import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

type ShutdownEvent = {
  code?: number;
  exitCode?: number;
  status?: number;
};

function getExitCode(event: ShutdownEvent | undefined): number {
  if (typeof event?.code === "number") {
    return event.code;
  }
  if (typeof event?.exitCode === "number") {
    return event.exitCode;
  }
  if (typeof event?.status === "number") {
    return event.status;
  }
  return 0;
}

export default function dgovDoneSignal(pi: ExtensionAPI) {
  pi.on("session_shutdown", async (event: ShutdownEvent) => {
    try {
      const slug = process.env.DGOV_SLUG;
      const sessionRoot = process.env.DGOV_SESSION_ROOT;

      if (!slug || !sessionRoot) {
        return;
      }

      const status = await pi.exec("git", ["status", "--porcelain"]);
      if (status.code === 0 && status.stdout.trim()) {
        await pi.exec("git", ["add", "-A"]);
        await pi.exec("git", [
          "commit",
          "-m",
          "dgov: auto-commit on worker exit",
        ]);
      }

      const doneDir = `${sessionRoot}/.dgov/state/done`;
      const isAbnormal = getExitCode(event) !== 0;
      const signalPath = `${doneDir}/${slug}${isAbnormal ? ".exit" : ""}`;

      await pi.exec("mkdir", ["-p", doneDir]);
      await pi.exec("touch", [signalPath]);
    } catch {
      // Shutdown hooks must not block session teardown.
    }
  });
}
