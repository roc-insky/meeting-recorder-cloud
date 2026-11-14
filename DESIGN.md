# Design System — meeting-recorder 云端版

日期：2026-11-14
状态：v0.1 视觉建议稿，待用户确认；不代表已应用到现有产品。
产品与系统设计：[云端设计文档](docs/product/cloud-meeting-recorder-design.md)。

## Product Context

- **What this is：** 浏览器登录的个人会议录音与面试准备工作台，支持麦克风及经授权的电脑播放声音采集。
- **Who it's for：** 中文会议记录者和技术面试官（定位建议）。
- **Space/industry：** 会议记录、语音转写、个人生产力。
- **Project type：** Web app，长文阅读与状态监控并重，不是营销网站或团队仪表盘。
- **最高原则：** 状态真实、录音可靠、个人数据隔离优先于装饰。

## Aesthetic Direction

- **Direction：** Industrial / Editorial 混合的可信录音工作台。
- **Decoration level：** minimal；依靠文字、边界、音源信号轨形成层级。
- **Mood：** 安静、清晰、可核对。用户三秒内应知道当前记录、两种音源和已保存位置。
- **Reference sites：** 本轮没有竞品调研；依据当前产品结构与设计知识，不伪称外部研究。
- **Safe：** 保留三栏结构、固定录音条、可预测表单和确认弹窗。
- **Risk 1：** 常驻双音源信号条；换取真实采集可见性，付出顶栏空间。
- **Risk 2：** 文字稿按文稿而非聊天气泡排列；换取阅读密度，付出聊天式亲切感。
- **Risk 3：** 精确显示连续云端保存时间；换取可信度，付出解释录制/上传差异的成本。

## Typography

- **Display/Hero：** Source Sans 3 + Noto Sans SC 中文；清晰而不过度强调营销感。
- **Body：** Noto Sans SC，拉丁字符使用 Source Sans 3，适合中文长文。
- **UI/Labels：** 同正文，500/600字重。
- **Data/Tables：** JetBrains Mono 时间戳与数字，启用 tabular-nums；姓名用正文。
- **Code：** JetBrains Mono；用户界面不默认展示底层代码。
- **Loading：** 正式环境自托管许可字体，按语言子集拆分，font-display: swap。离线回退 PingFang SC / Microsoft YaHei / sans-serif；数字回退 monospace。
- **Scale：** 页面标题28px/36px，区块标题20px/28px，次级标题18px/26px，正文16px/26px，控件14px/20px，辅助13px/20px，时间戳12px/20px。正文不以缩小字号换三栏密度。

## Color

- **Approach：** restrained，中性暖灰与深青绿操作色；语义状态专用。
- **Primary：** #0F766E，主要操作、当前导航；白色文字。
- **Secondary：** #1D4ED8，链接及电脑声音标识；不与主要按钮争夺层级。
- **Neutrals：** background #F6F7F5；surface #FFFFFF；surface-muted #EDF1EE；border #D4DDD7；text #17231D；muted #52615A。
- **Semantic：** success #166534 / bg #DCFCE7；warning #92400E / bg #FEF3C7；error #B91C1C / bg #FEE2E2；info #1E40AF / bg #DBEAFE。
- **Recording：** #B91C1C；录音使用红点+“正在录音”文字，不用红色闪烁或单靠颜色。
- **Dark mode：** background #111814；surface #19231D；surface-muted #223028；border #405248；text #EDF5EF；muted #B2C4B7；primary #5EEAD4（配深色文字#11231E）；secondary/info #93C5FD；success #86EFAC；warning #FCD34D；error/recording #FCA5A5。重新设计表面层级，不直接反色。
- **Contrast：** 目标WCAG AA：普通文字4.5:1，大字3:1，控件/焦点3:1；实际组件组合上线前验证。浅色border不单独作为关键输入边界，关键输入边界使用muted色。
- 音源、上传、处理状态都配图标/文字，不依赖色觉区分；次级色不可替代错误语义。

## Spacing

- **Base unit：** 4px。
- **Density：** 控件适度紧凑，文字稿舒适。
- **Scale：** 2xs 2 / xs 4 / sm 8 / md 16 / lg 24 / xl 32 / 2xl 48 / 3xl 64px。
- 文稿段间16px，面板内16–24px，页面边距24px（窄屏16px）。
- 关键点击目标44px，图标有可访问名称；密集列表不只靠悬停显示操作。

## Layout

- **Approach：** grid-disciplined app + editorial transcript。
- **Grid：** ≥1280px 三栏（历史240px、中央minmax(480px,1fr)、标注320px）；768–1279px 历史栏可折叠、标注抽屉；<768px 单栏、按文字稿/标注分区导航。
- **Max content width：** 工作台1680px；文字稿阅读宽度760px；表单640px；登录表单420px。
- **Topbar：** 56px；活动录音条最少64px、可换行，不能遮挡正文焦点。
- **Border radius：** sm 4px，md 8px，lg 12px（弹窗），full 9999px（小状态标识）。不所有组件统一大圆角。
- 桌面每栏独立滚动，文稿自动滚动只在用户处于底部时开启。
- 账号标识清晰，当前页面记录与仍活动的录音分开显示，避免跨记录混淆。

## Motion

- **Approach：** minimal-functional。
- **Easing：** enter ease-out；exit ease-in；move ease-in-out。
- **Duration：** micro 80ms；short 160ms；medium 240ms；long不默认使用。
- 状态变化采用文字替换+柔和过渡；不以不断播放的动画表达处理成功。
- prefers-reduced-motion关闭非必要过渡；计时/音量不每帧触发屏幕阅读器播报。

## Component and State Rules

- **按钮：** 主操作实色；次级描边；危险操作文字+明确确认。禁用原因就近解释。
- **音源卡：** 显示权限/轨道状态、音量、共享范围；“有轨”不等于“录到了预期声音”。
- **保存指示：** 显示连续ACK对应时间，不用任意最大序号；未上传与部分音频不可显示“全部保存”。
- **文字稿：** 时间、姓名、内容对齐；疑似身份常驻标签，不能只放tooltip。
- **错误：** 核心录音/上传错误持久显示，不能仅瞬时toast；AI失败不覆盖基础录音成功状态。
- **空状态：** 给出创建或恢复操作，不用虚构统计数据。
- **表单：** 固定label、清晰验证、键盘操作；简历区域提示不保存，声纹采集显示授权说明。
- **焦点：** 2px主色外框+2px间隙；弹窗限制焦点，关闭后回触发点。Esc取消声纹采集。
- **隐私：** 不在公开封面显示真实记录、简历或声纹信息；示例用虚构内容。
- **禁止：** 紫色渐变、装饰blob、三列圆形图标营销网格、所有内容居中、渐变主要按钮、无意义数字卡片。

## Decisions Log

| 日期 | 决策 | 依据与状态 |
|---|---|---|
| 2026-11-14 | 双音源与私有工作空间为核心 | 用户已明确需求 |
| 2026-11-14 | 保留历史/文稿/标注心智 | 依据现有产品，布局细节待确认 |
| 2026-11-14 | 青绿、暖灰、文稿阅读、功能动效 | 设计建议，尚未视觉确认 |
| 2026-11-14 | 本轮交付文档，不修改产品样式 | 用户要求完整设计文档 |
| 2026-11-14 | 竞品研究、HTML预览、正式规范锁定未执行 | 不把本轮文档输出等同完整视觉咨询验证 |

## Adoption Gate

此文件是云端版视觉草案，不是已批准的强制规范。用户确认后再作为云端UI实现依据；不得擅自覆盖本地版本。当前未创建仓库强制指令文件，避免把未确认设计写成对未来代码的约束。
