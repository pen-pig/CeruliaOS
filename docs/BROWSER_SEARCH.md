# 浏览器：搜索与表单脚本

## 本地启动

- **独立启动**：用于整盘写入 U 盘的原始镜像是 `usb-image.bin`；不要把 VMware 的 VMDK 或开发用的 `os-image.bin` 当成 U 盘镜像。写盘会清空所选盘。Browser 的 DNS、TCP、TLS、HTML 解析和绘制都运行在 CeruliaOS 内；不连接 Windows 浏览器。实机联网仍取决于系统是否有该网卡驱动。

- **QEMU**：双击 `build_and_test.bat`，或运行 `build_and_test_ninja.bat`；默认用 E1000 和 QEMU 内置的 NAT/DHCP，正常分配 `10.0.2.15`。修改构建配置后要关掉旧 QEMU 再启动，运行中的实例不会换网卡。QEMU 继续使用原来的 `disk.vhd`。
- **VMware**：运行 `build_and_test.bat vmware` 后，在 VMware Workstation 打开 `vmware/cerulia.vmx`。这个命令不启动 QEMU，不覆盖旧的 `ezos.vmx`。新版虚拟机使用独立的 `cerulia-data.vmdk` 保存用户数据；重编会保留已有数据盘，重编前须完全关机，不能处于挂起或快照状态。

在桌面按 **B** 打开 Browser，按 **F1** 输入 `bing.com` 再回车。页面加载后点击地址栏下方的搜索框（也可按 **Tab / F4**），输入关键词，按 **Enter** 或点 **Go**。结果中的链接可以点击；**F2 / F3** 后退、前进，**PgUp / PgDn** 翻页。地址栏和网页输入框是两个不同的焦点。

- **链接和跳转**：支持较长的地址、相对路径、带查询的域名根地址和 UTF-8 路径。点击 `#片段` 会在当前页面查找元素 ID 或传统命名锚点；不重复下载页面，也不会把片段发给服务器。脚本生成的目标仍需要 JS 和 DOM。
- **洛谷公开题目样例**：在 HTTPS 题目页按 **End** 查看页面数据实际包含的输入输出样例，保留空格和换行。只读公开数据，不运行其中的代码，也不代表可以登录或提交评测。
- **中文显示**：内置 31,040 个 GNU Unifont 字形，覆盖简繁、扩展 A 和常见标点。网页按实际像素宽度断行。浏览器优先处理 UTF-8 BOM 和受支持的 HTTP 字符集；否则检查 HTML 前 1024 字节的 `<meta charset>` 或 `http-equiv=Content-Type` 声明。GBK、GB2312、Windows-936 会转换成 UTF-8；尚不支持完整 GB18030、Big5 或中文输入法。
- **CSS**：识别外链 `rel=stylesheet`、内嵌 `<style>` 和行内 `style`。支持标签、ID、类、后代和直接子元素选择器；按文档顺序、选择器优先级和 `!important` 层叠。实现颜色、字号、继承、基本媒体查询，以及常规文档流里的宽度、最小高度、水平居中、单层 PNG/JPEG 背景和单行 Flex 排版。外链背景的相对 URL 以最终样式表地址为基准。**F6 / F7** 缩小或放大网页正文；**F8** 切换阅读模式。CSS 异步到达时，页内锚点会重新定位，手动滚动后不再抢位置。没有完整盒模型、固定/绝对定位、Flex 换行、Grid、背景平铺、多层背景、动画、网页字体、`@import` 或完整 CSS 语法。

- **表单**：使用页面提供的表单，不内置目标站点地址。GET 支持一个命名的 text/search 输入框；同源 HTTPS POST 支持一个 text/email/search 输入框和一个密码输入框，以及隐藏字段、默认提交按钮和 URL 编码。密码以星号显示，不进入 URL 或浏览历史。Tab 切字段，Enter 从用户名进入密码框或提交。还不支持多字段复杂表单、文件上传和中文输入法。
- **会话**：原生 Browser 只在证书通过验证的 HTTPS 导航中接收 Cookie。Cookie 在内存中按主机、端口和路径隔离，处理过期和删除；不跨域共享，不发给图片、CSS 或 shell fetch。跨源跳转不转发会话 Cookie；不向另一源重发 307/308 的 POST 正文。关闭浏览器、锁屏或切换桌面用户会清除会话。不是完整的现代浏览器 Cookie 实现。
- **外链 JavaScript**：内置 [QuickJS](https://bellard.org/quickjs/)，在 CeruliaOS 每个文档的独立用户态进程中运行。可按顺序执行同源经典外链脚本，共享全局环境，支持 Promise 和 `async/await`。浏览器接口目前只有按 ID 查找元素、读取渲染器解析出的文本、写入纯文本和 `DOMContentLoaded`。脚本不能直接读写文件、启动程序、访问网络或读取 Cookie；超时、跳页或崩溃不提交脚本写入。没实现的类型（例如模块、带 SRI 的脚本）不执行；有 CSP 时保守地停用脚本。阅读模式不启动新的脚本任务。
- **内联表单脚本**：短内联脚本与 `onsubmit` 仍使用单独的受限实现，支持字符串拼接、字段 `value`、ASCII 大小写转换和 trim，以及 `return false` 取消提交。无法执行的提交处理器会提示而不是擅自提交。
- **边界**：没有模块加载、完整 DOM、持续事件循环或 `fetch` / XHR。普通 HTTPS POST 已通过受控测试，但依赖 JavaScript 生成表单和网络请求的登录流程（如洛谷）还未打通；也不会绕过网站的验证码。

## 在 USB 键鼠上操作

把标准 Boot HID 键盘、鼠标直接接在根端口，再启动系统。支持键盘 Caps Lock、Shift 与 Alt+Tab；不是所有键盘的 Caps Lock 指示灯都会同步。鼠标位于前台 Browser 正文时可滚轮上下浏览，PgUp / PgDn 翻动一屏并保留一行上下文。鼠标描述符明确声明按钮、相对 X/Y 和 8 位滚轮时启用四字节报告；其他 Boot 鼠标使用三字节报告，不猜测滚轮字段。驱动支持一层 USB 2.0 Hub、根端口热插拔与标准绝对定位 USB tablet；不保证厂商自定义报告。

## 回归验证

- `python tests/test_html_probe.py`：宿主解析对拍和表单/脚本边界测试（Windows 需在已初始化 MSVC 的终端运行）。
- `python tests/test_browser_network.py`：用来宾桌面提交给受控 HTTP 服务，检查实际收到的查询串和点击链接的后续请求，并验证证书拒绝与慢服务器下的桌面响应。
- `python tests/test_browser_css.py --live`：只从模拟 U 盘启动；像素级验证外链和行内层叠、媒体查询、字号变更后的链接命中和 CSP；`--live` 额外验证两个站实际加载样式表。
- `python tests/test_browser_boxes.py --live`：在模拟 U 盘中验证样式表重定向后的背景路径、Flex 两列及间距、缩略解码的大 JPEG、CSS 重排后的锚点。在线检查另测三角洲主站 CSS 背景；`--cdn-jpeg` 单独启用其 CDN 的直链 JPEG 探针。
- `python tests/test_browser_session.py`：构建后在 Windows 运行（需 `cryptography`）；用临时 CA 和隔离镜像真实测试 TLS、密码掩码、POST、重定向、Cookie 及关闭后清理。临时信任根和测试 DNS 不写入产品镜像，也不使用真实网站账号。
- `python tests/test_usb_browser_input.py`：关闭 i8042，UEFI 从模拟 U 盘启动，只通过 xHCI USB 键鼠登录和操作长网页；加 `--uhci` 用 UHCI 的两个根端口连接键鼠。
- `python tests/test_browser_js.py`：仅从模拟 U 盘启动，校验两个脚本的下载顺序、同一文档的脚本共享状态及 DOM 读写。与预先排好的页面逐像素对比，并验证死循环时原页面不受影响、CSP 阻止脚本请求。
- 加 `--live`：另测真实 Bing 首页和搜索结果；需能访问外网，不作为离线门槛。
- 加 `--df --luogu`：检查两个实际站点的公开浏览流程（不登录、不提交评测、不下载或运行客户端）；需要外网。两站的交互功能如登录、评测、视频播放不属于静态浏览能力。
- Windows：生成 `build_auto.ninja` 后可加 `--launcher`，用批处理实际生成的网卡和时钟参数测试，不占用正式数据盘。

## 两站验收标准

Browser 在系统内用 Wuffs 解码 PNG 和常规 JPEG。基线 JPEG 超出完整解码缓冲时，用 TJpgDec 分块缩略解码，不分配一张全尺寸位图。先绘制文字，再按视窗加载图片；同类资源的相同引用在同一页不重复请求。内嵌资源仍受下载缓冲和物理内存约束；标题栏直接打开图片仍共用 256 KiB 文档缓冲。大型渐进 JPEG 没有分块缩略解码，SVG、WebP、动画和视频尚未支持。

子资源逐跳校验：跨源图片只连接 HTTPS/443，并过滤本地地址；不发送凭据。未解析的响应头 CSP 禁止取图；meta CSP 仅在明确允许网络图片（例如 `img-src * data:`）时放行，其他策略保守处理。不因取图而放宽脚本或表单权限。

运行 `python tests/test_browser_images.py` 从模拟 U 盘启动，检查来宾帧缓冲里的 PNG/JPEG 像素、透明背景、点击、去重及策略。加 `--live` 再检查两站的真实首页或主站图片。网页“能打开”、有图片与可登录、刷题、看视频是不同验收项。

- [洛谷](https://luogu.com.cn/)：公开浏览至少能跟随域名重定向、打开题库、点击题目并读到正文。日常刷题还要能搜索、登录、提交代码和查看评测结果；不要把仅能读题记为通过。
- [三角洲行动官网](https://df.qq.com/)：公开浏览至少能正确显示 GBK 首页、通过“进入官网”进入 UTF-8 主站。公告列表、动态内容和视频需要分别验收；网页兼容性不等于能启动游戏客户端。
