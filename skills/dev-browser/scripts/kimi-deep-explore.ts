import { connect, waitForPageLoad } from "@/client.js";

type ExploreResult = {
  status: "success" | "error";
  question: string;
  answer?: string;
  error?: string;
  timestamp: string;
};

function nowIso(): string {
  return new Date().toISOString();
}

function normalizeText(text: string): string {
  return (text || "")
    .replace(/\r\n/g, "\n")
    .replace(/\u00a0/g, " ")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function printAndExit(result: ExploreResult, exitCode: number): never {
  console.log(JSON.stringify(result));
  process.exit(exitCode);
}

async function screenshotSafe(page: any, path: string) {
  try {
    await page.screenshot({ path, fullPage: true });
  } catch {
    // ignore
  }
}

async function clickIfVisible(locator: any, timeoutMs = 800): Promise<boolean> {
  try {
    if (await locator.isVisible({ timeout: timeoutMs })) {
      await locator.click({ timeout: 3000 });
      return true;
    }
  } catch {
    // ignore
  }
  return false;
}

async function ensureOnKimi(page: any) {
  const url = page.url() || "";
  // Kimi 域名已迁移: kimi.moonshot.cn -> www.kimi.com
  if (!url.includes("kimi.moonshot.cn") && !url.includes("kimi.com")) {
    await page.goto("https://kimi.com/", {
      waitUntil: "domcontentloaded",
      timeout: 60000,
    });
  }
  await waitForPageLoad(page, { timeout: 20000 });

  // 弹窗/提示（尽力处理）
  await clickIfVisible(
    page.getByRole("button", { name: /同意|接受|我知道了|继续|OK|好的/i }).first()
  );
}

async function startNewChatIfPossible(page: any) {
  // Kimi 左侧栏通常有“新建会话”入口
  const newChat = page.getByText("新建会话").first();
  if (await clickIfVisible(newChat, 1200)) {
    await page.waitForTimeout(600);
  }
}

async function fillQuestion(page: any, question: string) {
  // Kimi 输入框是 contenteditable div.chat-input-editor
  const editor = page.locator(".chat-input-editor").first();
  await editor.waitFor({ state: "visible", timeout: 15000 });
  await editor.click({ timeout: 3000 });

  // 清空并输入
  await page.keyboard.press("Meta+A");
  await page.keyboard.press("Backspace");
  await page.keyboard.type(question, { delay: 8 });
}

async function sendQuestion(page: any) {
  // 发送按钮没有明确 aria-label，但有稳定的 class
  const send = page
    .locator(".send-button-container:not(.disabled) .send-button, .send-button")
    .first();

  try {
    await send.waitFor({ state: "visible", timeout: 10000 });
    await send.click({ timeout: 5000 });
  } catch {
    // 兜底：尝试 Enter
    await page.keyboard.press("Enter");
  }
}

async function waitForAssistantAnswer(
  page: any,
  beforeCount: number,
  timeoutMs: number
) {
  const start = Date.now();
  const answers = page.locator(".segment-assistant .markdown");

  // 等待新的回答块出现
  while (Date.now() - start < timeoutMs) {
    try {
      const count = await answers.count();
      if (count > beforeCount) break;
    } catch {
      // ignore
    }
    await page.waitForTimeout(400);
  }

  // 追踪最后一个回答，等待文本稳定
  let lastText = "";
  let stable = 0;
  while (Date.now() - start < timeoutMs) {
    try {
      const count = await answers.count();
      if (count > 0) {
        const text = normalizeText(await answers.nth(count - 1).innerText());
        if (text && text === lastText) stable += 1;
        else stable = 0;
        lastText = text;

        // 一般流式输出会逐步增长；连续几次不变即可认为完成
        if (lastText.length >= 80 && stable >= 4) {
          return lastText;
        }
      }
    } catch {
      // ignore
    }

    // 若存在“停止生成”按钮，说明仍在生成
    try {
      const stopBtn = page.getByRole("button", { name: /停止|Stop/i }).first();
      if (await stopBtn.isVisible({ timeout: 200 })) {
        stable = 0;
      }
    } catch {
      // ignore
    }

    await page.waitForTimeout(800);
  }

  return lastText;
}

async function main() {
  const question = normalizeText(process.argv.slice(2).join(" "));
  if (!question) {
    printAndExit(
      { status: "error", question: "", error: "缺少问题参数", timestamp: nowIso() },
      2
    );
  }

  const client = await connect();
  const pageName = "kimi-deep-explore";

  try {
    const page = await client.page(pageName);
    await page.setViewportSize({ width: 1280, height: 860 });

    await ensureOnKimi(page);
    await startNewChatIfPossible(page);

    // 若被登录页拦住，提前报错（避免空跑）
    const bodyText = await page.evaluate(() => document.body?.innerText || "");
    const bodyLower = String(bodyText).toLowerCase();
    if (
      bodyLower.includes("登录") &&
      (bodyLower.includes("手机号") || bodyLower.includes("验证码"))
    ) {
      await screenshotSafe(page, "tmp/kimi_login_required.png");
      printAndExit(
        {
          status: "error",
          question,
          error: "Kimi 页面疑似要求登录（已截图 tmp/kimi_login_required.png）。请先在该浏览器 Profile 中完成登录后重试。",
          timestamp: nowIso(),
        },
        1
      );
    }

    const answers = page.locator(".segment-assistant .markdown");
    const beforeCount = await answers.count().catch(() => 0);

    await fillQuestion(page, question);
    await sendQuestion(page);

    const answer = await waitForAssistantAnswer(page, beforeCount, 120000);

    if (!answer || answer.length < 50) {
      await screenshotSafe(page, "tmp/kimi_answer_extract_failed.png");
      printAndExit(
        {
          status: "error",
          question,
          error: "未能提取到有效回答（已截图 tmp/kimi_answer_extract_failed.png）。可能是页面结构变化、发送失败或回答未生成完成。",
          timestamp: nowIso(),
        },
        1
      );
    }

    printAndExit(
      {
        status: "success",
        question,
        answer,
        timestamp: nowIso(),
      },
      0
    );
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    try {
      const page = await client.page(pageName);
      await screenshotSafe(page, "tmp/kimi_deep_explore_error.png");
    } catch {
      // ignore
    }
    printAndExit(
      {
        status: "error",
        question,
        error: `${msg}（已尝试截图 tmp/kimi_deep_explore_error.png）`,
        timestamp: nowIso(),
      },
      1
    );
  } finally {
    await client.disconnect();
  }
}

await main();

