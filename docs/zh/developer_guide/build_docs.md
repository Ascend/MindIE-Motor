# 本地构建 MkDocs 文档服务

本文档说明仓库内 **MkDocs** 的布局、本地预览与静态构建方式，以及在 **`docs/zh`** 下新增/修改 Markdown 页面时需要同步修改的配置（尤其是 **`docs/zh/.nav.yaml`**）。

站点入口配置在仓库根目录的 **`mkdocs.yml`**（`docs_dir` 指向 **`docs/zh`**）。侧边栏导航不由 `mkdocs.yml` 中的 `nav` 字段维护，而是由 **`mkdocs-awesome-nav`** 插件读取 **`docs/zh/.nav.yaml`** 生成。

## MkDocs 相关目录结构

下文路径均以仓库根目录为基准。

```text
MindIE-PyMotor/
├── mkdocs.yml                    # MkDocs 主配置（主题、插件、extensions、extra_css/js 等）
├── .readthedocs.yaml             # Read the Docs 在线构建（Python 版本、依赖安装、fail_on_warning）
├── requirements/
│   └── mkdocs.txt                # 文档构建专用 pip 依赖
├── docs/
│   ├── mkdocs/                   # 文档工程资源（hooks、自定义样式与脚本），逻辑上的「canonical」目录
│   │   ├── hooks/                # on_page_markdown 等钩子（见下文）
│   │   ├── stylesheets/
│   │   │   └── extra.css         # 全局额外样式（由 mkdocs.yml 的 extra_css 引用）
│   │   └── javascripts/
│   │       └── mathjax.js        # MathJax 配置脚本（由 mkdocs.yml 的 extra_javascript 引用）
│   └── zh/                       # MkDocs 的文档根目录（mkdocs.yml 中的 docs_dir）
│       ├── mkdocs/               # → 指向 ../mkdocs 的符号链接，便于 extra_css/js 路径落在 docs_dir 内并被正确拷贝
│       ├── .nav.yaml             # 侧边栏导航树（awesome-nav）
│       ├── index.md              # 站点首页
│       ├── assets/               # 主题引用的静态资源（如 logo、favicon，见 mkdocs.yml）
│       ├── user_guide/           # 用户指南等 Markdown 正文目录（按需分子目录）
│       ├── developer_guide/
│       ├── api_reference/
│       └── ...
└── site/                         # mkdocs build 默认输出目录（通常勿提交版本库）
```

说明要点：

- **正文 Markdown** 一律放在 **`docs/zh/`** 下（含子目录）；MkDocs 只会把该目录树当作文档源扫描（再配合 `.nav.yaml` 决定导航）。
- **`docs/mkdocs/`** 存放与「文档站点工程」相关的脚本与静态资源；**`docs/zh/mkdocs`** 链到 **`docs/mkdocs`**，这样 `extra_css` / `extra_javascript` 可使用形如 **`mkdocs/stylesheets/extra.css`** 的路径（相对于 **`docs/zh`**），构建产物中的静态 URL 才能正确对应。**若在 Windows 克隆仓库且符号链接异常**，需在 **`docs/zh`** 下自行维护等价链接或拷贝，保证上述路径下文件存在。
- **`docs/mkdocs/hooks/`** 中的 Python 文件在 **`mkdocs.yml` 的 `hooks:`** 中注册；不属于 Markdown 页面，也不会出现在导航里。

## 新增一篇 Markdown 文档时要做什么

1. **创建文件**
   在 **`docs/zh/`** 下选定分类目录（例如 `user_guide/`、`developer_guide/`），新建 **`*.md`**。路径应尽量与文档类型一致，便于检索与评审。

2. **更新导航（必选）**
   编辑 **`docs/zh/.nav.yaml`**，在合适的层级增加一行 **`显示标题: 相对 docs/zh 的路径`**。
   - 仅新增文件而不改 `.nav.yaml` 时，页面可通过 URL 直达（若未被插件排除），但**侧边栏不会出现**，也容易在门禁链接检查中与「文档入口不一致」。
   - 同级条目顺序即为侧边栏顺序；多级目录使用 YAML 嵌套（见下一节）。

3. **自查链接与锚点**
   - 站内页面链接建议使用 **相对于 `docs/zh` 的路径**，例如 **`/user_guide/quick_start.md`**（与 MkDocs/Material 的文档根解析一致），或与当前文件相对的 **`deployment/k8s/config_reference.md`**。门禁 **`link-validity-check`** 常以 **`docs/zh`** 为基准校验本地 Markdown 链接，避免出现仓库根路径、`../../../examples/...` 等无法在站点内解析的目标。
   - 可使用 **`mkdocs serve --strict`** 或 **`mkdocs build --strict`**，将链接失效、缺失锚点等告警暴露出来。

4. **可选：配图与其它静态文件**
   图片等若放在 **`docs/zh`** 下，请使用相对当前 Markdown 的路径引用；若使用 **`docs/mkdocs/hooks/img_width.py`** 支持的 HTML `<img width="...">` 写法，构建时会转为 Material 可用的属性语法。

## 导航配置（`.nav.yaml`）

插件 **`mkdocs-awesome-nav`** 读取 **`docs/zh/.nav.yaml`**，顶层一般为：

```yaml
nav:
  - 首页: index.md
  - 某分组标题:
      - 子页面标题: path/under/docs_zh.md
      - 更深分组:
          - 条目: nested/page.md
```

规则摘要：

- **键（左侧中文）**：侧边栏展示标题。
- **值（右侧路径）**：相对于 **`docs/zh`** 的 Markdown 路径，使用 **`/`** 分隔，**不要**写 **`docs/zh/`** 前缀。
- **嵌套**：用 **`某分组标题:`** 下一层缩进的列表表示多级目录；只有叶子节点需要绑定 **`*.md`** 文件。

新增文档后，把对应条目加到最接近的分类下即可；无需修改 **`mkdocs.yml`** 中的 `nav`（本仓库未使用该字段维护导航）。

## 环境与钩子说明

### 环境要求

- **Python**：建议 **3.11**（与 **`.readthedocs.yaml`** 在线构建一致）。
- **依赖清单**：**`requirements/mkdocs.txt`**，主要包括 **`mkdocs`**、**`mkdocs-material`**、**`mkdocs-awesome-nav`**、**`mkdocs-glightbox`**、**`mkdocs-git-revision-date-localized-plugin`**、**`mkdocs-minify-plugin`**、**`mkdocs-redirects`**、**`pymdown-extensions`**。
- **钩子**：根目录 **`docs/mkdocs/hooks/`**（在 **`mkdocs.yml`** 的 **`hooks:`** 中启用）：
    - **`github_admonition.py`**：把 GitHub/Obsidian 风格的 **`>[!NOTE]`** / **`> [!NOTE]`** 引用块转成 pymdown 的 **`!!! note`**，便于 Material 渲染提示框；引用块内若嵌套 **` ``` `** 代码围栏，请避免 **`>` 与围栏之间多空格**，以免闭合围栏错位导致 **`#` 注释行被当成 Markdown 标题**。
    - **`img_width.py`**：把 **`<img src="..." width="...">`** 转为 **`![](){ width="..." }`** 以便控制宽度。

### 编写提示（易踩坑）

- **围栏代码语言**： fenced 代码块语言标识请使用 Pygments 支持的名称（例如纯文本使用 **`text`**，不要使用 **`Plain Text`**）。
- **Material 专有语法**：源码中的 **`!!! note`** 仅在 MkDocs 中渲染；若希望 GitHub 等通用预览也可读，可优先使用 **`>[!NOTE]`** 交给钩子转换（避免仅两个空格之类的「装饰缩进」贴在 **`>[!NOTE]`** 前，否则可能影响解析）。
- **`extra_css` / `extra_javascript`**：在 **`mkdocs.yml`** 中配置为相对于 **`docs/zh`** 的路径（当前为 **`mkdocs/stylesheets/extra.css`**、**`mkdocs/javascripts/mathjax.js`**），依赖 **`docs/zh/mkdocs`** 指向 **`docs/mkdocs`**。

## 安装依赖

在仓库根目录执行：

```shell
pip install -r requirements/mkdocs.txt
```

## 启动本地实时预览

Step 1：在项目根目录执行以下命令启动 MkDocs 本地服务（会监听文件变更并热重载）：

```shell
mkdocs serve
```

Step 2：启动成功后，终端将输出类似以下信息：

```text
INFO     -  Building documentation...
INFO     -  Cleaning site directory
INFO     -  Documentation built in 1.23 s
INFO     -  [12:00:00] Watching paths for changes
INFO     -  [12:00:00] Serving on http://127.0.0.1:8000/
```

Step 3：在浏览器中访问终端给出的地址即可预览文档（若配置了多语言或版本路径，请以实际输出为准）。

> [!NOTE]说明
>
> * **`mkdocs serve`** 默认监听 **`8000`** 端口。若该端口被占用，可通过 **`-a`** 指定其它地址端口，例如 **`mkdocs serve -a 127.0.0.1:8080`**。
> * 想在本地尽早发现链接/锚点等问题，可加 **`--strict`**：**`mkdocs serve --strict`**。Read the Docs 当前配置为 **`fail_on_warning: false`**（见 **`.readthedocs.yaml`**），但本地仍可主动开启以减少静默错误。

## 静态构建（产出可发布站点）

如需生成可托管到静态服务器（如 Nginx、对象存储）的站点产物：

```shell
mkdocs build
```

产物默认输出到仓库根目录的 **`site/`**。可将告警视为错误：

```shell
mkdocs build --strict
```

在线文档构建流程见 **`.readthedocs.yaml`**（使用 **`requirements/mkdocs.txt`** 安装依赖并读取根目录 **`mkdocs.yml`**）。

## 常见问题

### 依赖安装失败

若 **`pip install`** 过程中出现依赖冲突或安装失败，建议使用虚拟环境隔离：

```shell
python -m venv .venv_mkdocs
source .venv_mkdocs/bin/activate
pip install -r requirements/mkdocs.txt
```

### YAML 解析报错或 `!!python/name:` 标签异常

**`mkdocs.yml`** 中 **`pymdownx.superfences`**、**`pymdownx.emoji`** 等扩展使用了 **`!!python/name:`** YAML 标签，与 PyYAML **`safe_load`** 不兼容。**`pre-commit`** 里的 **`check-yaml`** 已对 **`mkdocs.yml`** 做 **`exclude`**（见仓库 **`.pre-commit-config.yaml`**）；本地其它 YAML 校验若报警告，可对 **`mkdocs.yml`** 同样排除。

### 导航没有按预期更新

本仓库导航由 **`docs/zh/.nav.yaml`** 驱动。新增页面后请在该文件中补充条目并核对 YAML 缩进；保存后重新 **`mkdocs serve`** 即可刷新侧边栏。

### `extra.css` / `mathjax.js` 预览 404

若 **`mkdocs/stylesheets/extra.css`** 或 **`mkdocs/javascripts/mathjax.js`** 请求返回 **404**，请确认 **`docs/zh/mkdocs`** 是否为指向 **`docs/mkdocs`** 的有效符号链接，且 **`mkdocs.yml`** 中的路径相对于 **`docs/zh`** 无误。

### 站内链接门禁报错「本地路径无法访问」

门禁常以 **`docs/zh`** 为根解析 Markdown 中的本地链接。请避免指向 **`docs/zh`** 之外的 **`*.md`**（除非改为站内路径或将内容纳入 **`docs/zh`**）；跨仓库示例目录的说明可放在 **`examples/`** 并由正文给出仓库内路径文字说明，或使用 **`repo_url`** 上的浏览链接。
