import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

export default function (pi: ExtensionAPI) {
    const protectedPaths = [
        "CLAUDE.md",
        ".dgov/",
        ".git/",
        ".env",
        "node_modules/",
        "HANDOVER.md",
    ];

    pi.on("tool_call", async (event, ctx) => {
        if (event.toolName !== "write" && event.toolName !== "edit") {
            return undefined;
        }
        const path = event.input.path as string;
        const isProtected = protectedPaths.some((p) => path.includes(p));
        if (isProtected) {
            if (ctx.hasUI) {
                ctx.ui.notify("Blocked write to protected path: " + path, "warning");
            }
            return { block: true, reason: "Path " + path + " is protected" };
        }
        return undefined;
    });
}
