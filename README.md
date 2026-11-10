# muse-spaces 发布说明（给 Muse）

每次更新完 muse.ai 页面并核对无误后，再把同一份伙计的 markdown 发布到这个 GitHub Pages 仓库。成功后再发微信通知。

## 地址（固定，可直接在微信里打开，不用登录）

- 首页：https://rizen7.github.io/muse-spaces/
- 国内外资讯：https://rizen7.github.io/muse-spaces/news/
- 薅羊毛：https://rizen7.github.io/muse-spaces/deals/

仓库：https://github.com/Rizen7/muse-spaces

## 发布

在 box 上运行（路径不要改）：

```bash
/home/box/muse-spaces/publish.sh news /workspace/news-space/YYYY-MM-DD.md
/home/box/muse-spaces/publish.sh deals /workspace/deals-space/YYYY-MM-DD-0900.md
```

下午那场薅羊毛用 `YYYY-MM-DD-1400.md`。

脚本会：渲染单文件 HTML → 提交并推送到 `Rizen7/muse-spaces` 的 `main` → 循环检查线上地址，直到 HTTP 200 且页面含有本次「更新于」时间（最多等 5 分钟）。

- 成功：标准输出一行 `{"ok":true,"url":"..."}`，退出码 0。这时再发微信。
- 失败：标准输出 `{"ok":false,...}`，退出码非 0。不要发带链接的成功通知；把失败原因记下来。

不要加 `--sample`。那个参数只用于首版试跑，会在页面顶部写「试跑样例」。

源文件不存在，或第一行是「状态：失败」（不含「部分失败」）时，页面会显示「本次刷新失败」，但仍会带「更新于」时间并推送。

## 发微信通知

发布成功后再发，用：

```bash
/home/box/wxb-run/send-owner.sh --file <通知文案路径>
```

通知写短一些，附上对应 GitHub Pages 链接（不要用 muse.ai 链接，微信里打不开）。

若 `send-owner.sh` 退出码为 3，表示微信连接断开：**不要再发**，改到 Grok 聊天里提醒用户重新扫码。

## 换样式

页面是单文件 HTML，样式和模板是分开的，换设计时优先只改文件、不要改占位符名字：

- 样式：`styles/common.css`（公共）、`styles/news.css`、`styles/deals.css`、`styles/index.css`
- 模板：`templates/news.html`、`templates/deals.html`、`templates/index.html`

类名和 `{{title}}`、`{{css}}`、`{{content}}` 等占位符被 `render.py` 使用，改模板时请保留。`publish.sh` 渲染时会把 CSS 内联进 HTML，不引用外部 CDN 或字体文件。
