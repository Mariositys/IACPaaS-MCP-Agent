import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { createTool } from "@cline/sdk";

const READ_TOOLS = new Set(["get_inforesource", "get_subgraph", "count_graph", "query_graph"]);
const WRITE_TOOLS = new Set(["import_inforesource", "update_vertex", "delete_inforesource"]);
const MAX_EVIDENCE_ENTRIES = 50;
const MAX_EVIDENCE_CONVERSATIONS = 50;
const MAX_VALIDATOR_OUTPUT = 256 * 1024;
const evidenceByConversation = new Map();

const RULE = `# IACPaaS

- Для вопросов о содержимом инфоресурсов используй MCP-тулзы iacpaas.
- Имена тулзов: iacpaas__get_inforesource, iacpaas__get_subgraph, iacpaas__count_graph, iacpaas__query_graph.
- Каждый финальный ответ обязан быть отправлен через submit_answer.
- Не вызывай submit_and_exit или attempt_completion.
- В claims каждое утверждение должно ссылаться на call_id, который вернул MCP-тулз, и на JSON Pointer.
- Если данных не хватает, отправь submit_answer с refused=true и пустым claims.
- Не добавляй факты, чисел и цитаты, которых нет в результатах тулзов.`;

function bareToolName(name) {
  const index = name.lastIndexOf("__");
  return index >= 0 ? name.slice(index + 2) : name;
}

function isIacpaasTool(name) {
  return name.startsWith("iacpaas__");
}

function isReadTool(name) {
  return isIacpaasTool(name) && READ_TOOLS.has(bareToolName(name));
}

function isWriteTool(name) {
  return isIacpaasTool(name) && WRITE_TOOLS.has(bareToolName(name));
}

function evidenceKeyFromSnapshot(snapshot) {
  return snapshot?.conversationId ?? snapshot?.runId ?? null;
}

function evidenceKeyFromContext(context) {
  return context?.conversationId ?? context?.runId ?? context?.sessionId ?? null;
}

function evidenceFor(key) {
  let evidence = evidenceByConversation.get(key);
  if (!evidence) {
    if (evidenceByConversation.size >= MAX_EVIDENCE_CONVERSATIONS) {
      const oldest = evidenceByConversation.keys().next().value;
      if (oldest !== undefined) evidenceByConversation.delete(oldest);
    }
    evidence = new Map();
    evidenceByConversation.set(key, evidence);
  }
  return evidence;
}

function rememberEvidence(key, callId, value) {
  const evidence = evidenceFor(key);
  if (!evidence.has(callId) && evidence.size >= MAX_EVIDENCE_ENTRIES) {
    const oldest = evidence.keys().next().value;
    if (oldest !== undefined) evidence.delete(oldest);
  }
  evidence.set(callId, value);
}

function citedEvidence(journal, claims) {
  const cited = new Set();
  for (const claim of Array.isArray(claims) ? claims : []) {
    for (const source of Array.isArray(claim?.sources) ? claim.sources : []) {
      if (typeof source?.call_id === "string") cited.add(source.call_id);
    }
  }
  const evidence = {};
  for (const [callId, value] of journal) {
    if (cited.has(callId)) evidence[callId] = value;
  }
  return evidence;
}

function unwrapMcpResult(output) {
  if (output && typeof output === "object") {
    if (output.structuredContent && typeof output.structuredContent === "object") {
      return output.structuredContent.result ?? output.structuredContent;
    }
    if (Array.isArray(output.content)) {
      const text = output.content.find((part) => part?.type === "text")?.text;
      if (typeof text === "string") {
        try {
          return JSON.parse(text);
        } catch {
          return text;
        }
      }
    }
  }
  return output;
}

function resolveProjectRoot(context) {
  if (process.env.IACPAAS_PROJECT_ROOT) return process.env.IACPAAS_PROJECT_ROOT;
  const root = context?.workspaceInfo?.rootPath || context?.cwd || process.cwd();
  if (existsSync(join(root, "pyproject.toml"))) return root;
  const nested = join(root, "IACPaaS-MCP-Agent");
  if (existsSync(join(nested, "pyproject.toml"))) return nested;
  return root;
}

function killChild(child) {
  if (!child.pid) return;
  if (process.platform === "win32") {
    spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], {
      windowsHide: true,
      stdio: "ignore",
    }).on("error", () => {
      try {
        child.kill();
      } catch {}
    });
    return;
  }
  try {
    child.kill("SIGKILL");
  } catch {}
}

function runValidator(root, payload) {
  return new Promise((resolve, reject) => {
    let serialized;
    try {
      serialized = JSON.stringify(payload);
    } catch (error) {
      reject(new Error(`Не удалось сериализовать ответ: ${error.message}`));
      return;
    }

    const venvPython =
      process.platform === "win32"
        ? join(root, ".venv", "Scripts", "python.exe")
        : join(root, ".venv", "bin", "python");
    const useVenv = existsSync(venvPython);
    const command = useVenv ? venvPython : "uv";
    const args = useVenv
      ? ["-m", "iacpaas_agent.bridge"]
      : ["run", "--no-sync", "python", "-m", "iacpaas_agent.bridge"];

    let child;
    try {
      child = spawn(command, args, {
        cwd: root,
        env: { ...process.env, PYTHONIOENCODING: "utf-8" },
        stdio: ["pipe", "pipe", "pipe"],
        windowsHide: true,
      });
    } catch (error) {
      reject(new Error(`Не удалось запустить валидатор: ${error.message}`));
      return;
    }

    let stdout = "";
    let stderr = "";
    let settled = false;
    let timer;

    const finish = (handler, value) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      handler(value);
    };

    const fail = (message) => {
      killChild(child);
      finish(reject, new Error(message));
    };

    timer = setTimeout(() => fail("Валидатор не ответил за 60 секунд"), 60000);

    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      if (settled) return;
      stdout += chunk;
      if (stdout.length > MAX_VALIDATOR_OUTPUT) {
        fail("Валидатор вернул слишком большой stdout");
      }
    });
    child.stderr.on("data", (chunk) => {
      if (settled) return;
      stderr += chunk;
      if (stderr.length > MAX_VALIDATOR_OUTPUT) {
        fail("Валидатор вернул слишком большой stderr");
      }
    });
    child.on("error", (error) => {
      finish(reject, new Error(`Не удалось запустить валидатор: ${error.message}`));
    });
    child.on("close", (code) => {
      if (settled) return;
      if (code !== 0) {
        finish(
          reject,
          new Error(`Валидатор завершился с кодом ${code}: ${stderr.trim().slice(0, 1000)}`),
        );
        return;
      }
      try {
        finish(resolve, JSON.parse(stdout.trim()));
      } catch {
        finish(reject, new Error(`Валидатор вернул не JSON: ${stdout.slice(0, 300)}`));
      }
    });
    child.stdin.on("error", (error) => {
      if (error.code !== "EPIPE") {
        fail(`Ошибка записи в валидатор: ${error.message}`);
      }
    });
    child.stdin.end(serialized, "utf8");
  });
}

function makeSubmitAnswer(root) {
  return createTool({
    name: "submit_answer",
    description:
      "Вернуть окончательный проверенный ответ и завершить run. Каждый claim обязан ссылаться на call_id из результата MCP-тулза и JSON Pointer.",
    inputSchema: {
      type: "object",
      properties: {
        answer: { type: "string", description: "Текст ответа пользователю" },
        refused: { type: "boolean", description: "true, если данных не хватило" },
        claims: {
          type: "array",
          items: {
            type: "object",
            properties: {
              statement: { type: "string" },
              value: { type: ["string", "number", "boolean", "null"] },
              quote: { type: ["string", "null"] },
              sources: {
                type: "array",
                minItems: 1,
                items: {
                  type: "object",
                  properties: {
                    call_id: { type: "string" },
                    pointer: { type: "string" },
                    expr: { type: ["string", "null"] },
                  },
                  required: ["call_id", "pointer"],
                },
              },
            },
            required: ["statement", "sources"],
          },
        },
      },
      required: ["answer", "refused", "claims"],
    },
    lifecycle: { completesRun: true },
    async execute(input, context) {
      const key = evidenceKeyFromContext(context);
      if (!key) {
        throw new Error("Не удалось определить сессию для проверки evidence");
      }
      const evidence = citedEvidence(evidenceFor(key), input.claims);
      const verdict = await runValidator(root, {
        answer: input,
        evidence,
      });
      if (!verdict.ok) {
        throw new Error("Проверка не прошла:\n- " + (verdict.reasons ?? []).join("\n- "));
      }
      return {
        answer: verdict.final,
        claims: input.claims,
        verified: true,
      };
    },
  });
}

const hooks = {
  beforeTool(context) {
    const name = context.toolCall.toolName;
    if (name === "submit_and_exit" || name === "attempt_completion") {
      return {
        skip: true,
        reason: "Финальный ответ принимается только через submit_answer после проверки.",
      };
    }
    if (isWriteTool(name)) {
      return {
        skip: true,
        reason: "В этом режиме IACPaaS доступен только для чтения.",
      };
    }
    return undefined;
  },
  afterTool(context) {
    const name = context.toolCall.toolName;
    if (!isReadTool(name)) return undefined;
    const callId = context.toolCall.toolCallId;
    if (!callId) return undefined;
    const key = evidenceKeyFromSnapshot(context.snapshot);
    if (!key) return undefined;

    const normalized = unwrapMcpResult(context.result.output);
    rememberEvidence(key, callId, normalized);

    const output =
      normalized && typeof normalized === "object" && !Array.isArray(normalized)
        ? { ...normalized, call_id: callId }
        : { value: normalized, call_id: callId };
    return { result: { ...context.result, output } };
  },
};

const plugin = {
  name: "iacpaas-spike",
  manifest: {
    capabilities: ["tools", "hooks", "rules", "mcp"],
  },
  setup(api, context) {
    const projectRoot = resolveProjectRoot(context);
    context?.logger?.log?.("[iacpaas] plugin loaded", {
      severity: "info",
      projectRoot,
    });
    api.registerMcpServer({
      name: "iacpaas",
      transport: {
        type: "stdio",
        command: "uv",
        args: ["run", "--no-sync", "iacpaas-mcp"],
        cwd: projectRoot,
      },
    });
    api.registerRule({
      id: "iacpaas.contract",
      content: RULE,
    });
    api.registerTool(makeSubmitAnswer(projectRoot));
  },
  hooks,
};

export default plugin;
