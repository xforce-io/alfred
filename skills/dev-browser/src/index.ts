/**
 * Dev-Browser Server 模块
 * 
 * 这个文件是 dev-browser 的核心服务端实现，负责：
 * 1. 启动并管理 Chromium 浏览器进程（通过 Playwright）
 * 2. 提供 HTTP API 用于页面管理（创建、列出、删除页面）
 * 3. 维护页面注册表，将用户友好的名称映射到浏览器页面
 * 4. 管理浏览器状态持久化（cookies、localStorage、缓存等）
 * 
 * 架构说明：
 * - Server 是长期运行的后台进程，管理浏览器生命周期
 * - Client 通过 HTTP API 和 CDP 协议与 Server 通信
 * - 页面状态在 Server 中持久化，即使 Client 断开连接也保持活跃
 */

import express, { type Express, type Request, type Response } from "express";
import { chromium, type BrowserContext, type Page } from "playwright";
import { mkdirSync } from "fs";
import { join } from "path";
import type { Socket } from "net";
import type {
  ServeOptions,
  GetPageRequest,
  GetPageResponse,
  ListPagesResponse,
  ServerInfoResponse,
} from "./types";

// 导出类型定义，供其他模块使用
export type { ServeOptions, GetPageResponse, ListPagesResponse, ServerInfoResponse };

/**
 * DevBrowserServer 接口
 * 
 * 表示一个运行中的 dev-browser 服务器实例
 * 
 * @property wsEndpoint - CDP WebSocket 端点地址，Client 通过这个地址连接浏览器
 * @property port - HTTP API 服务器监听的端口号
 * @property stop - 停止服务器的异步方法，会清理所有资源
 */
export interface DevBrowserServer {
  wsEndpoint: string;
  port: number;
  stop: () => Promise<void>;
}

/**
 * 带重试机制的 fetch 函数（指数退避策略）
 * 
 * 为什么需要这个函数？
 * - 浏览器启动需要时间，CDP 端点可能不会立即可用
 * - 网络请求可能因为临时问题失败
 * - 使用指数退避避免频繁重试造成资源浪费
 * 
 * @param url - 要请求的 URL
 * @param maxRetries - 最大重试次数（默认 5 次）
 * @param delayMs - 初始延迟时间（毫秒），每次重试会递增
 * @returns Promise<Response> - fetch 响应对象
 * 
 * 重试策略：
 * - 第 1 次失败后等待 500ms
 * - 第 2 次失败后等待 1000ms
 * - 第 3 次失败后等待 1500ms
 * - ...以此类推
 */
async function fetchWithRetry(
  url: string,
  maxRetries = 5,
  delayMs = 500
): Promise<globalThis.Response> {
  let lastError: Error | null = null;
  for (let i = 0; i < maxRetries; i++) {
    try {
      const res = await fetch(url);
      if (res.ok) return res;
      throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    } catch (err) {
      lastError = err instanceof Error ? err : new Error(String(err));
      if (i < maxRetries - 1) {
        // 指数退避：延迟时间 = delayMs * (重试次数 + 1)
        await new Promise((resolve) => setTimeout(resolve, delayMs * (i + 1)));
      }
    }
  }
  throw new Error(`Failed after ${maxRetries} retries: ${lastError?.message}`);
}

/**
 * 为 Promise 添加超时保护
 * 
 * 为什么需要这个函数？
 * - 某些操作（如创建页面）可能因为浏览器问题而卡住
 * - 超时保护可以避免程序无限等待
 * - 提供清晰的超时错误信息
 * 
 * @param promise - 要添加超时的 Promise
 * @param ms - 超时时间（毫秒）
 * @param message - 超时时显示的错误消息
 * @returns Promise<T> - 带超时保护的 Promise
 * 
 * 工作原理：
 * - 使用 Promise.race 让原 Promise 和超时 Promise 竞争
 * - 哪个先完成就返回哪个的结果
 * - 如果超时 Promise 先完成，则抛出超时错误
 */
function withTimeout<T>(promise: Promise<T>, ms: number, message: string): Promise<T> {
  return Promise.race([
    promise,
    new Promise<never>((_, reject) =>
      setTimeout(() => reject(new Error(`Timeout: ${message}`)), ms)
    ),
  ]);
}

/**
 * 启动 dev-browser 服务器
 * 
 * 这是整个系统的核心函数，负责：
 * 1. 启动 Chromium 浏览器（使用持久化上下文）
 * 2. 启用 CDP（Chrome DevTools Protocol）远程调试
 * 3. 创建 HTTP API 服务器用于页面管理
 * 4. 维护页面注册表
 * 
 * @param options - 服务器配置选项
 * @param options.port - HTTP API 端口（默认 9222）
 * @param options.headless - 是否无头模式运行（默认 false，显示浏览器窗口）
 * @param options.cdpPort - CDP 协议端口（默认 9223）
 * @param options.profileDir - 浏览器配置文件目录（可选）
 * @returns Promise<DevBrowserServer> - 服务器实例
 * 
 * 双端口设计说明：
 * - port (9222): HTTP API，用于页面管理（RESTful，无状态）
 * - cdpPort (9223): CDP WebSocket，用于浏览器控制（有状态，长连接）
 * 
 * 为什么需要两个端口？
 * - 职责分离：HTTP 管理页面生命周期，CDP 控制浏览器行为
 * - 解耦设计：HTTP 服务挂掉，CDP 连接仍然可用
 * - 灵活性：可以用 curl 查看页面列表，同时 Playwright 连接不受影响
 */
export async function serve(options: ServeOptions = {}): Promise<DevBrowserServer> {
  // ========== 第一步：解析和验证配置参数 ==========
  
  const port = options.port ?? 9222;           // HTTP API 端口
  const headless = options.headless ?? false;   // 是否无头模式（false = 显示浏览器窗口）
  const cdpPort = options.cdpPort ?? 9223;     // CDP 协议端口
  const profileDir = options.profileDir;        // 浏览器配置文件目录（可选）

  // 验证端口号范围（1-65535 是有效端口范围）
  if (port < 1 || port > 65535) {
    throw new Error(`Invalid port: ${port}. Must be between 1 and 65535`);
  }
  if (cdpPort < 1 || cdpPort > 65535) {
    throw new Error(`Invalid cdpPort: ${cdpPort}. Must be between 1 and 65535`);
  }
  // 确保两个端口不同（避免冲突）
  if (port === cdpPort) {
    throw new Error("port and cdpPort must be different");
  }

  // ========== 第二步：设置浏览器数据持久化目录 ==========
  
  /**
   * 浏览器数据目录说明：
   * - 这个目录存储浏览器的所有持久化数据：cookies、localStorage、IndexedDB、缓存等
   * - 使用持久化目录的好处：即使服务器重启，登录状态、用户数据都会保留
   * - 如果指定了 profileDir，使用指定目录；否则使用当前工作目录下的 .browser-data
   */
  const userDataDir = profileDir
    ? join(profileDir, "browser-data")
    : join(process.cwd(), ".browser-data");

  // 创建目录（如果不存在）
  // recursive: true 表示如果父目录不存在也会创建
  mkdirSync(userDataDir, { recursive: true });
  console.log(`Using persistent browser profile: ${userDataDir}`);

  // ========== 第三步：启动浏览器（持久化上下文） ==========
  
  console.log("Launching browser with persistent context...");

  /**
   * launchPersistentContext vs launch 的区别：
   * 
   * launch() - 临时浏览器：
   *   - 每次启动都是全新的浏览器
   *   - 关闭后所有数据丢失（cookies、localStorage 等）
   *   - 适合一次性脚本
   * 
   * launchPersistentContext() - 持久化浏览器：
   *   - 使用指定的用户数据目录
   *   - 自动保存和恢复所有浏览器状态
   *   - 关闭后数据仍然保留在磁盘上
   *   - 适合需要保持状态的场景（如登录状态）
   * 
   * args 参数说明：
   * - --remote-debugging-port: 启用 CDP 远程调试协议
   *   这个端口允许外部程序（如 Playwright Client）通过 WebSocket 连接控制浏览器
   */
  const context: BrowserContext = await chromium.launchPersistentContext(userDataDir, {
    headless,  // false = 显示浏览器窗口，true = 无头模式（不显示窗口）
    args: [`--remote-debugging-port=${cdpPort}`],  // 启用 CDP 协议
  });
  console.log("Browser launched with persistent profile...");

  // ========== 第四步：获取 CDP WebSocket 端点 ==========
  
  /**
   * CDP（Chrome DevTools Protocol）说明：
   * 
   * 【重要概念澄清】
   * 
   * CDP 本身是一个**协议规范**（Protocol Specification），类似于 HTTP 协议规范。
   * 但是，当我们说"启动 CDP"时，实际上是指：
   * 
   * 1. **CDP 协议** = 通信规范（定义命令格式、数据结构等）
   *    - 类似于 HTTP 协议规范（RFC 2616）
   *    - 定义了如何与浏览器通信的格式
   * 
   * 2. **CDP 服务器** = 实现该协议的服务器（内置在浏览器中）
   *    - 类似于 HTTP 服务器（如 Apache、Nginx）
   *    - 浏览器启动时会启动一个内置的 CDP 服务器
   *    - 监听指定端口，等待外部程序连接
   * 
   * 【工作流程】
   * 
   * 1. 浏览器启动时，如果指定了 `--remote-debugging-port=9223`：
   *    - 浏览器会启动内置的 CDP 服务器
   *    - CDP 服务器监听端口 9223
   *    - 同时提供一个 HTTP 端点用于发现服务（/json/version）
   * 
   * 2. 外部程序（如 Playwright）连接：
   *    - 先访问 HTTP 端点获取 WebSocket 地址
   *    - 通过 WebSocket 连接到 CDP 服务器
   *    - 发送 CDP 命令（JSON-RPC 格式）
   * 
   * 3. CDP 服务器处理命令：
   *    - 解析 CDP 命令
   *    - 执行相应操作（导航、点击等）
   *    - 返回结果
   * 
   * 【类比理解】
   * 
   * - HTTP 协议规范 vs HTTP 服务器（Apache）
   * - CDP 协议规范 vs CDP 服务器（浏览器内置）
   * 
   * 当我们说"启动 CDP"时，实际上是指"启动浏览器的 CDP 服务器"
   * 
   * 【在代码中的体现】
   * 
   * - `--remote-debugging-port=${cdpPort}`: 启动浏览器的 CDP 服务器
   * - `http://127.0.0.1:${cdpPort}/json/version`: CDP 服务器提供的发现端点
   * - `wsEndpoint`: CDP 服务器的 WebSocket 地址
   * 
   * 为什么需要重试？
   * - 浏览器启动需要时间，CDP 服务器可能不会立即可用
   * - 使用 fetchWithRetry 确保在 CDP 服务器完全启动后再获取端点
   */
  const cdpResponse = await fetchWithRetry(`http://127.0.0.1:${cdpPort}/json/version`);
  const cdpInfo = (await cdpResponse.json()) as { webSocketDebuggerUrl: string };
  const wsEndpoint = cdpInfo.webSocketDebuggerUrl;
  console.log(`CDP WebSocket endpoint: ${wsEndpoint}`);

  // ========== 第五步：创建页面注册表 ==========
  
  /**
   * 页面注册表说明：
   * 
   * 为什么需要注册表？
   * - 用户使用友好的名称（如 "login-page"、"dashboard"）来标识页面
   * - 但浏览器内部使用 targetId（如 "B2F3A1..."）来标识页面
   * - 注册表建立名称到页面的映射关系
   * 
   * 双重标识系统：
   * 1. 用户层面：字符串名称（易读、易用）
   * 2. 系统层面：CDP targetId（全局唯一、稳定）
   * 
   * 设计优势：
   * - 用户可以给页面起有意义的名称
   * - 跨会话引用：重启客户端后可以通过相同名称找回页面
   * - 简化脚本：无需在脚本中传递复杂的 ID
   */
  interface PageEntry {
    page: Page;           // Playwright Page 对象，用于操作页面
    targetId: string;     // CDP target ID，用于跨连接识别页面
  }

  /**
   * 注册表：页面名称 -> PageEntry
   * 
   * 【Map 数据类型说明】
   * 
   * registry 是一个 Map 对象，用于存储键值对映射关系。
   * 
   * 【Map 是什么？】
   * 
   * Map 是 JavaScript ES6（ECMAScript 2015）引入的原生数据结构。
   * - ✅ JavaScript 原生：不是 Node.js 特有的，浏览器中也支持
   * - ✅ 标准特性：所有现代 JavaScript 环境都支持
   * - ✅ 类型安全：TypeScript 提供类型支持
   * 
   * 【语法说明】
   * 
   * ```typescript
   * const registry = new Map<string, PageEntry>();
   * ```
   * 
   * - `new Map()`: 创建新的 Map 实例
   * - `<string, PageEntry>`: TypeScript 泛型，指定键和值的类型
   *   - 键（Key）类型：string（页面名称）
   *   - 值（Value）类型：PageEntry（包含 page 和 targetId 的对象）
   * 
   * 【Map vs Object（普通对象）】
   * 
   * | 特性 | Map | Object |
   * |------|-----|--------|
   * | 键的类型 | 任意类型（string, number, object等） | 只能是 string 或 symbol |
   * | 大小 | 通过 .size 属性获取 | 需要手动计算 |
   * | 迭代顺序 | 按插入顺序 | ES6+ 按插入顺序（但有限制） |
   * | 性能 | 大量增删操作更快 | 少量数据时更快 |
   * | 原型链 | 无原型链，更安全 | 有原型链，可能被污染 |
   * 
   * 【为什么选择 Map？】
   * 
   * 1. **类型安全**：
   *    ```typescript
   *    // Map 可以明确指定键值类型
   *    const map = new Map<string, PageEntry>();
   *    map.set("login", entry);  // ✅ TypeScript 会检查类型
   *    ```
   * 
   * 2. **性能优势**：
   *    - O(1) 的查找、插入、删除性能
   *    - 适合频繁的增删操作
   * 
   * 3. **清晰的 API**：
   *    ```typescript
   *    map.set(key, value);    // 设置
   *    map.get(key);            // 获取
   *    map.has(key);            // 检查是否存在
   *    map.delete(key);         // 删除
   *    map.size;                // 获取大小
   *    map.clear();             // 清空
   *    ```
   * 
   * 4. **避免原型链污染**：
   *    ```typescript
   *    // Object 的问题
   *    const obj = {};
   *    obj.toString = "hack";  // ❌ 可能覆盖原型方法
   *    
   *    // Map 没有这个问题
   *    const map = new Map();
   *    map.set("toString", "safe");  // ✅ 安全，不会影响原型
   *    ```
   * 
   * 【在代码中的使用】
   * 
   * ```typescript
   * // 创建
   * const registry = new Map<string, PageEntry>();
   * 
   * // 设置值（第 728 行）
   * registry.set(name, entry);
   * 
   * // 获取值
   * let entry = registry.get(name);
   * 
   * // 检查是否存在
   * if (registry.has(name)) { ... }
   * 
   * // 删除
   * registry.delete(name);
   * 
   * // 获取所有键（第 481 行）
   * Array.from(registry.keys())
   * 
   * // 获取所有值
   * Array.from(registry.values())
   * ```
   * 
   * 【Map 数据结构提供 O(1) 的查找性能】
   * 
   * O(1) 表示时间复杂度为常数时间，即：
   * - 无论 Map 中有多少元素
   * - 查找、插入、删除操作的时间都是固定的
   * - 比数组的 O(n) 查找快得多
   */
  const registry = new Map<string, PageEntry>();

  /**
   * 获取页面的 CDP targetId
   * 
   * targetId 是什么？
   * - CDP 协议中每个页面都有一个唯一的 targetId
   * - 即使 Playwright 客户端断开重连，targetId 依然有效
   * - 用于在多个 context 中精确定位页面
   * 
   * 为什么需要这个函数？
   * - Client 需要通过 targetId 在重连后找到对应的页面
   * - targetId 比页面 URL 更可靠（URL 可能变化）
   * 
   * @param page - Playwright Page 对象
   * @returns Promise<string> - 页面的 CDP targetId
   * 
   * CDP Session 说明：
   * - newCDPSession: 创建一个 CDP 会话，用于发送 CDP 命令
   * - send("Target.getTargetInfo"): 发送 CDP 命令获取页面信息
   * - detach(): 关闭会话（必须在 finally 中确保执行）
   */
  async function getTargetId(page: Page): Promise<string> {
    const cdpSession = await context.newCDPSession(page);
    try {
      // 发送 CDP 命令获取页面信息
      const { targetInfo } = await cdpSession.send("Target.getTargetInfo");
      return targetInfo.targetId;
    } finally {
      // 确保会话被关闭，避免资源泄漏
      await cdpSession.detach();
    }
  }

  // ========== 第六步：创建 HTTP API 服务器 ==========
  
  /**
   * Express 服务器说明：
   * 
   * Express 是 Node.js 的 Web 框架，用于创建 HTTP API
   * 这里我们创建一个 RESTful API 用于页面管理
   * 
   * API 设计：
   * - GET /          : 获取服务器信息（CDP WebSocket 端点）
   * - GET /pages      : 列出所有页面名称
   * - POST /pages     : 获取或创建页面（幂等操作）
   * - DELETE /pages/:name : 关闭指定页面
   */
  const app: Express = express();
  
  /**
   * 中间件：自动解析 JSON 请求体
   * 
   * 【什么是中间件？】
   * 中间件（Middleware）是在请求到达路由处理函数之前执行的函数。
   * 可以把它想象成"请求处理流水线"中的一个环节。
   * 
   * 【express.json() 的作用】
   * 这个中间件会自动解析 HTTP 请求体（body）中的 JSON 数据，
   * 并将解析后的 JavaScript 对象放到 req.body 中。
   * 
   * 【为什么需要它？】
   * 
   * 1. HTTP 请求体是原始字符串：
   *    当客户端发送 POST 请求时，数据是以字符串形式传输的：
   *    ```
   *    POST /pages HTTP/1.1
   *    Content-Type: application/json
   *    
   *    {"name": "login-page"}  ← 这是字符串，不是对象
   *    ```
   * 
   * 2. 需要手动解析（没有中间件时）：
   *    ```typescript
   *    app.post("/pages", (req, res) => {
   *      let body = '';
   *      req.on('data', chunk => {
   *        body += chunk.toString();
   *      });
   *      req.on('end', () => {
   *        const data = JSON.parse(body);  // 手动解析
   *        const { name } = data;
   *        // 处理逻辑...
   *      });
   *    });
   *    ```
   * 
   * 3. 使用 express.json() 后：
   *    ```typescript
   *    app.use(express.json());  // 注册中间件
   *    
   *    app.post("/pages", (req, res) => {
   *      const { name } = req.body;  // 自动解析好了！
   *      // 直接使用，无需手动解析
   *    });
   *    ```
   * 
   * 【工作流程】
   * 
   * 1. 客户端发送请求：
   *    ```bash
   *    curl -X POST http://localhost:9222/pages \
   *      -H "Content-Type: application/json" \
   *      -d '{"name": "login-page"}'
   *    ```
   * 
   * 2. express.json() 中间件拦截请求：
   *    - 检查 Content-Type 是否为 application/json
   *    - 读取请求体的原始字符串：'{"name": "login-page"}'
   *    - 使用 JSON.parse() 解析字符串
   *    - 将结果放到 req.body：{ name: "login-page" }
   * 
   * 3. 路由处理函数接收：
   *    ```typescript
   *    app.post("/pages", (req, res) => {
   *      const body = req.body;  // { name: "login-page" }
   *      const { name } = body;   // "login-page"
   *      // 直接使用，无需手动解析
   *    });
   *    ```
   * 
   * 【在代码中的实际使用】
   * 
   * 看第 397-399 行：
   * ```typescript
   * app.post("/pages", async (req: Request, res: Response) => {
   *   const body = req.body as GetPageRequest;  // ← 这里直接使用 req.body
   *   const { name } = body;                     // ← 已经是解析好的对象
   * ```
   * 
   * 如果没有 `app.use(express.json())`，req.body 会是 undefined！
   * 
   * 【app.use() 的作用】
   * 
   * app.use() 用于注册中间件，中间件会：
   * - 对所有匹配的请求执行（如果不指定路径，则对所有请求执行）
   * - 按照注册顺序依次执行
   * - 可以修改 req 和 res 对象
   * - 可以调用 next() 继续下一个中间件或路由
   * 
   * 【类比理解】
   * 
   * 想象一个快递分拣中心：
   * - 快递（请求）到达
   * - express.json() 就像"自动拆包机"（中间件）
   * - 拆包机自动打开包裹，取出里面的物品（JSON 数据）
   * - 把物品放到 req.body 这个"篮子"里
   * - 然后交给下一个环节（路由处理函数）处理
   * 
   * 【注意事项】
   * 
   * 1. 必须在路由之前注册：
   *    ```typescript
   *    app.use(express.json());  // ✅ 正确：在路由之前
   *    app.post("/pages", ...);
   *    
   *    app.post("/pages", ...);  // ❌ 错误：在路由之后
   *    app.use(express.json());
   *    ```
   * 
   * 2. 只解析 Content-Type 为 application/json 的请求
   * 
   * 3. 如果 JSON 格式错误，会返回 400 错误
   */
  app.use(express.json());

  /**
   * 路由注册：GET / - 获取服务器信息
   * 
   * 【app.get() 是什么？】
   * 
   * app.get() 是 Express 的路由注册方法，用于注册处理 GET 请求的路由。
   * 
   * 语法：app.get(路径, 处理函数)
   * - 路径：URL 路径（如 "/", "/pages"）
   * - 处理函数：当请求匹配这个路径时执行的函数
   * 
   * 【什么是"注册"？】
   * 
   * "注册"的意思是：告诉 Express 服务器：
   * "当有人访问这个路径时，请执行这个函数"
   * 
   * 类比理解：
   * - 就像在电话簿上登记："如果有人打这个号码，转接给我"
   * - 或者像在餐厅点餐："如果有人点这道菜，用这个食谱做"
   * 
   * 【工作流程】
   * 
   * 1. 注册阶段（服务器启动时）：
   *    ```typescript
   *    app.get("/", (req, res) => {
   *      // 这个函数被"注册"到路径 "/" 上
   *      // 但此时还没有执行
   *    });
   *    ```
   * 
   * 2. 请求阶段（客户端访问时）：
   *    ```
   *    客户端 → GET http://localhost:9222/
   *              ↓
   *    Express 查找注册的路由
   *              ↓
   *    找到 app.get("/", ...) 注册的处理函数
   *              ↓
   *    执行处理函数
   *              ↓
   *    返回响应
   *    ```
   * 
   * 【HTTP 方法说明】
   * 
   * Express 提供了对应 HTTP 方法的注册方法：
   * - app.get()    → 处理 GET 请求（获取数据）
   * - app.post()   → 处理 POST 请求（创建数据）
   * - app.put()    → 处理 PUT 请求（更新数据）
   * - app.delete() → 处理 DELETE 请求（删除数据）
   * - app.patch()  → 处理 PATCH 请求（部分更新）
   * 
   * 【为什么需要不同的方法？】
   * 
   * 同一个路径可以注册不同的 HTTP 方法：
   * ```typescript
   * app.get("/pages", ...)     // 获取页面列表
   * app.post("/pages", ...)    // 创建新页面
   * app.delete("/pages/:id", ...) // 删除页面
   * ```
   * 
   * 这样可以根据 HTTP 方法执行不同的操作，符合 RESTful API 设计。
   * 
   * 【处理函数参数】
   * 
   * 处理函数接收两个参数：
   * - req (Request): 请求对象，包含请求信息
   *   - req.url: 请求的 URL
   *   - req.method: HTTP 方法
   *   - req.body: 请求体（POST/PUT 请求的数据）
   *   - req.params: URL 参数（如 /pages/:name 中的 name）
   *   - req.query: 查询字符串参数（如 ?page=1）
   * 
   * - res (Response): 响应对象，用于发送响应
   *   - res.json(): 发送 JSON 响应
   *   - res.send(): 发送文本响应
   *   - res.status(): 设置状态码
   * 
   * 【实际使用示例】
   * 
   * 这个路由的作用：
   * - 路径：GET /
   * - 用途：Client 首次连接时获取 CDP WebSocket 端点
   * - 响应：返回服务器信息（包括 WebSocket 地址）
   * 
   * 客户端访问：
   * ```bash
   * curl http://localhost:9222/
   * ```
   * 
   * 服务器响应：
   * ```json
   * {
   *   "wsEndpoint": "ws://127.0.0.1:9223/devtools/browser/..."
   * }
   * ```
   */
  app.get("/", (_req: Request, res: Response) => {
    const response: ServerInfoResponse = { wsEndpoint };
    res.json(response);
  });

  /**
   * 路由注册：GET /pages - 列出所有页面名称
   * 
   * 【路由注册说明】
   * 
   * 这是另一个路由注册的例子，展示了如何注册 GET 请求的路由。
   * 
   * 注册内容：
   * - 路径：/pages
   * - HTTP 方法：GET
   * - 处理函数：返回所有页面名称的列表
   * 
   * 【用途】
   * - 调试和状态检查
   * - 查看当前有哪些页面在运行
   * 
   * 【使用示例】
   * 
   * 客户端请求：
   * ```bash
   * curl http://localhost:9222/pages
   * ```
   * 
   * 服务器响应：
   * ```json
   * {
   *   "pages": ["login", "dashboard", "checkout"]
   * }
   * ```
   * 
   * 【代码说明】
   * 
   * - `registry.keys()`: 获取注册表中所有页面名称（Map 的键）
   * - `Array.from()`: 将迭代器转换为数组
   * - `res.json()`: 发送 JSON 格式的响应
   */
  app.get("/pages", (_req: Request, res: Response) => {
    const response: ListPagesResponse = {
      pages: Array.from(registry.keys()),  // 获取注册表中所有页面名称
    };
    res.json(response);
  });

  /**
   * 路由注册：POST /pages - 获取或创建页面（幂等操作）
   * 
   * 【app.post() 说明】
   * 
   * app.post() 用于注册处理 POST 请求的路由。
   * POST 请求通常用于创建资源或提交数据。
   * 
   * 【与 app.get() 的区别】
   * 
   * | 方法 | HTTP 方法 | 用途 | 是否有请求体 |
   * |------|-----------|------|-------------|
   * | app.get() | GET | 获取数据 | ❌ 通常没有 |
   * | app.post() | POST | 创建/提交数据 | ✅ 通常有 |
   * 
   * 【为什么用 POST 而不是 GET？】
   * 
   * 1. 需要发送数据（页面名称）：
   *    - GET 请求的数据在 URL 中（查询字符串）
   *    - POST 请求的数据在请求体中（更安全、更灵活）
   * 
   * 2. 语义更清晰：
   *    - GET /pages → 获取页面列表
   *    - POST /pages → 创建新页面
   * 
   * 3. 符合 RESTful 规范：
   *    - GET 用于读取
   *    - POST 用于创建
   * 
   * 【这是最核心的 API 端点】
   * 
   * 负责：
   * 1. 接收页面名称（从 req.body）
   * 2. 检查页面是否已存在（查询注册表）
   * 3. 如果不存在，创建新页面（调用 context.newPage()）
   * 4. 返回页面信息（包括 targetId）
   * 
   * 【幂等性说明】
   * 
   * 幂等性（Idempotent）：
   * - 多次调用相同的名称，返回同一个页面
   * - 不会创建重复的页面
   * - 第一次调用创建页面，后续调用返回已存在的页面
   * 
   * 示例：
   * ```typescript
   * // 第一次调用
   * POST /pages { name: "login" } → 创建页面，返回 targetId
   * 
   * // 第二次调用（相同名称）
   * POST /pages { name: "login" } → 返回已存在的页面，不创建新的
   * ```
   * 
   * 【请求和响应】
   * 
   * 请求体示例：
   * ```json
   * {
   *   "name": "login-page"
   * }
   * ```
   * 
   * 响应示例：
   * ```json
   * {
   *   "wsEndpoint": "ws://127.0.0.1:9223/...",
   *   "name": "login-page",
   *   "targetId": "B2F3A1..."
   * }
   * ```
   * 
   * 【async 关键字】
   * 
   * 处理函数使用 async，因为：
   * - 需要等待异步操作（创建页面、获取 targetId）
   * - 可以使用 await 关键字
   * - 返回 Promise，Express 会自动处理
   */
  app.post("/pages", async (req: Request, res: Response) => {
    const body = req.body as GetPageRequest;
    const { name } = body;

    // ========== 参数验证 ==========
    
    // 验证名称是否存在且为字符串
    if (!name || typeof name !== "string") {
      res.status(400).json({ error: "name is required and must be a string" });
      return;
    }

    // 验证名称不为空
    if (name.length === 0) {
      res.status(400).json({ error: "name cannot be empty" });
      return;
    }

    // 验证名称长度（256 字符限制基于文件系统路径长度限制）
    if (name.length > 256) {
      res.status(400).json({ error: "name must be 256 characters or less" });
      return;
    }

    // ========== 获取或创建页面 ==========
    
    // 检查页面是否已存在
    let entry = registry.get(name);
    if (!entry) {
      /**
       * 创建新页面流程：
       * 1. 在持久化上下文中创建新页面（标签页）
       * 2. 使用超时保护避免卡住（30 秒超时）
       * 3. 获取页面的 CDP targetId
       * 4. 注册到注册表中
       * 5. 监听页面关闭事件，自动清理注册表
       */
      
      // 创建新页面（带超时保护）
      const page = await withTimeout(
        context.newPage(), 
        30000, 
        "Page creation timed out after 30s"
      );
      
      // 获取页面的 CDP targetId（用于跨连接识别）
      const targetId = await getTargetId(page);
      
      // 创建注册表条目
      entry = { page, targetId };
      registry.set(name, entry);

      /**
       * 自动清理机制：
       * 
       * 当页面被关闭时（用户点击 X、脚本调用 close()、页面崩溃等），
       * 自动从注册表中删除，避免内存泄漏
       * 
       * 为什么需要这个？
       * - 如果用户手动关闭浏览器标签页，注册表应该同步更新
       * - 避免注册表中保留已关闭页面的引用
       */
      page.on("close", () => {
        registry.delete(name);
      });
    }

    // 返回页面信息（包括 CDP WebSocket 端点和 targetId）
    const response: GetPageResponse = { wsEndpoint, name, targetId: entry.targetId };
    res.json(response);
  });

  /**
   * DELETE /pages/:name - 关闭指定页面
   * 
   * 用途：显式关闭页面，释放资源
   * 
   * URL 示例：DELETE /pages/login-page
   * 
   * 响应示例（成功）：
   * {
   *   "success": true
   * }
   * 
   * 响应示例（页面不存在）：
   * {
   *   "error": "page not found"
   * }
   */
  app.delete("/pages/:name", async (req: Request<{ name: string }>, res: Response) => {
    // 解码 URL 参数（处理特殊字符）
    const name = decodeURIComponent(req.params.name);
    const entry = registry.get(name);

    if (entry) {
      // 关闭页面（这会触发上面的 "close" 事件，自动清理注册表）
      await entry.page.close();
      registry.delete(name);  // 双重保险：即使事件没触发也删除
      res.json({ success: true });
      return;
    }

    // 页面不存在，返回 404
    res.status(404).json({ error: "page not found" });
  });

  // ========== 第七步：启动 HTTP 服务器 ==========
  
  /**
   * 启动 Express 服务器，监听指定端口
   * 
   * 默认监听 localhost（127.0.0.1），只接受本地连接
   * 这是安全考虑：不应该暴露到公网
   */
  const server = app.listen(port, () => {
    console.log(`HTTP API server running on port ${port}`);
  });

  /**
   * 跟踪活跃的 HTTP 连接
   * 
   * 为什么需要这个？
   * - 优雅关闭时需要关闭所有活跃连接
   * - 避免连接在关闭过程中挂起
   * 
   * 工作原理：
   * - 监听 "connection" 事件，记录所有新连接
   * - 监听 "close" 事件，移除已关闭的连接
   */
  const connections = new Set<Socket>();
  server.on("connection", (socket: Socket) => {
    connections.add(socket);
    socket.on("close", () => connections.delete(socket));
  });

  // ========== 第八步：设置优雅关闭机制 ==========
  
  /**
   * 清理标志，防止重复清理
   * 
   * 为什么需要这个？
   * - 多个信号可能同时触发（如 SIGINT 和 uncaughtException）
   * - 防止清理函数被多次执行
   */
  let cleaningUp = false;

  /**
   * 清理函数：优雅关闭所有资源
   * 
   * 关闭顺序很重要：
   * 1. 关闭所有 HTTP 连接（避免请求挂起）
   * 2. 关闭所有页面（释放浏览器资源）
   * 3. 关闭浏览器上下文（这会关闭整个浏览器进程）
   * 4. 关闭 HTTP 服务器
   * 
   * 为什么使用 try-catch？
   * - 某些资源可能已经关闭（如用户手动关闭浏览器）
   * - 优雅处理错误，确保所有资源都能被清理
   */
  const cleanup = async () => {
    // 防止重复清理
    if (cleaningUp) return;
    cleaningUp = true;

    console.log("\nShutting down...");

    // 1. 关闭所有活跃的 HTTP 连接
    for (const socket of connections) {
      socket.destroy();  // 强制关闭连接
    }
    connections.clear();

    // 2. 关闭所有页面
    for (const entry of registry.values()) {
      try {
        await entry.page.close();
      } catch {
        // 页面可能已经关闭（用户手动关闭等）
      }
    }
    registry.clear();

    // 3. 关闭浏览器上下文（这会关闭整个浏览器进程）
    try {
      await context.close();
    } catch {
      // 上下文可能已经关闭
    }

    // 4. 关闭 HTTP 服务器
    server.close();
    console.log("Server stopped.");
  };

  /**
   * 同步清理函数：用于强制退出场景
   * 
   * 什么时候使用？
   * - process.on("exit") 事件是同步的，不能使用 async/await
   * - 作为最后一道防线，尽力清理资源
   * 
   * 为什么是"尽力而为"？
   * - 同步操作可能无法完全清理异步资源
   * - 但至少可以关闭浏览器进程
   */
  const syncCleanup = () => {
    try {
      context.close();  // 同步关闭（可能不完整，但尽力而为）
    } catch {
      // 忽略错误
    }
  };

  /**
   * 信号处理器：处理系统信号
   * 
   * 系统信号说明：
   * - SIGINT: Ctrl+C（中断信号）
   * - SIGTERM: 终止信号（如 kill 命令）
   * - SIGHUP: 挂起信号（终端关闭）
   * 
   * 为什么需要处理这些信号？
   * - 用户可能通过 Ctrl+C 停止服务器
   * - 系统可能发送终止信号
   * - 需要优雅关闭，而不是强制退出
   */
  const signals = ["SIGINT", "SIGTERM", "SIGHUP"] as const;

  /**
   * 信号处理函数：收到信号时优雅关闭
   */
  const signalHandler = async () => {
    await cleanup();
    process.exit(0);  // 正常退出
  };

  /**
   * 错误处理函数：处理未捕获的异常
   * 
   * 为什么需要这个？
   * - 防止未处理的错误导致进程崩溃
   * - 确保资源被正确清理
   */
  const errorHandler = async (err: unknown) => {
    console.error("Unhandled error:", err);
    await cleanup();
    process.exit(1);  // 异常退出
  };

  // 注册所有事件处理器
  signals.forEach((sig) => process.on(sig, signalHandler));
  process.on("uncaughtException", errorHandler);      // 未捕获的异常
  process.on("unhandledRejection", errorHandler);    // 未处理的 Promise 拒绝
  process.on("exit", syncCleanup);                    // 进程退出（最后防线）

  /**
   * 移除所有事件处理器
   * 
   * 什么时候使用？
   * - 调用 stop() 方法时
   * - 防止内存泄漏（事件监听器不会被自动垃圾回收）
   */
  const removeHandlers = () => {
    signals.forEach((sig) => process.off(sig, signalHandler));
    process.off("uncaughtException", errorHandler);
    process.off("unhandledRejection", errorHandler);
    process.off("exit", syncCleanup);
  };

  // ========== 返回服务器实例 ==========
  
  /**
   * 返回服务器实例，提供：
   * - wsEndpoint: CDP WebSocket 端点（供 Client 连接）
   * - port: HTTP API 端口
   * - stop(): 停止服务器的方法（用于程序化关闭）
   */
  return {
    wsEndpoint,
    port,
    async stop() {
      removeHandlers();  // 移除事件监听器
      await cleanup();   // 清理资源
    },
  };
}
