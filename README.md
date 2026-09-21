# SweetHomeMaid 汉化

DMM游戏「スイートホームメイド」SweetHomeMaid的机翻汉化。
使用用油猴脚本把游戏加载的剧情替换成中文。

## 使用方法

1. 浏览器安装油猴插件 [Tampermonkey](https://www.tampermonkey.net/)。  
   手机端可使用[Edge(推荐)](https://explore.microsoft.com/zh-cn/edge/mobile)、[火狐](https://www.firefox.com/zh-CN/download/android/)等支持安装插件的浏览器。  
   建议使用Edge，游玩流畅且支持将游戏页面作为`PWA`安装到桌面，体验上与使用官方app游玩无异。  
   火狐实测游玩大型的关卡时容易卡顿且只支持添加页面快捷方式到桌面，无法全屏游玩，除非另外安装全屏插件。
3. 打开 [汉化脚本](https://raw.githubusercontent.com/Mephistor666/SweetHomeMaidTranslate/main/SweetHomeMaidCN.user.js)，
   Tampermonkey 会弹出安装页，点「安装」。或者在 Tampermonkey 里手动添加新脚本，将SweetHomeMaidCN.user.js的内容全部复制替换到新脚本中，然后点左上角的文件->save。
4. 正常打开游戏玩。已翻译的剧情自动显示中文，汉化生效时右下角会提示 `汉化已生效：xxx`。
5. 该脚本还有一个功能可以去除寝室剧情的闪屏演出。点击Tampermonkey插件菜单的`去除闪光弹`选项即可生效，有三个子选项:`移除全部闪光(默认)`，`移除短闪`，`移除长闪`。游戏正常情况是播放两个快速的闪屏再放一个1s以上的长闪屏，可根据自身需求选用该功能。
6. 关闭汉化在 Tampermonkey 里禁用脚本即可。

## 注意事项

- 只有已翻译好并传到此库的剧情是中文，其余仍是日文，游戏更新后需要等新剧情的译文更新上传后才有效。
- 译文从 GitHub 读取，需要网络环境能访问 GitHub。

## 实现

- 汉化方式参考了童话边境的汉化方法 [alex343425/otogitranslate](https://github.com/alex343425/otogitranslate)，劫持xhr提交和返回的内容来替换原文为译文。
- 翻译模型：[Sakura LLM](https://github.com/SakuraLLM/SakuraLLM) Sakura-Galtransl-14B-v3.8-Q4_K_M
- 主页点角色的对话气泡和剧情标题使用deepseek-flash翻译。
- 脚本主体由deepseek-flash实现。

## 声明

- 全部译文为**机器翻译**，仅供学习交流，请勿商用。
- Sakura LLM 系列模型为 CC BY-NC-SA 4.0 协议，禁止商用。
- 游戏版权归原厂所有。发现问题或有想补的翻译，欢迎加群交流，QQ：596809681。  
<br/>
  
**P.S.** 新公会招人中，xxx，欢迎新老玩家加入，正常玩就行，长期不活跃的会清。
