# SweetHomeMaid 汉化

DMM游戏「スイートホームメイド」SweetHomeMaid的机翻汉化。
使用用油猴脚本把游戏加载的剧情替换成中文。

## 使用方法

1. 浏览器安装油猴插件，手机端的话火狐浏览器可以安装 [Tampermonkey](https://www.tampermonkey.net/)。
2. 打开 [汉化脚本](https://raw.githubusercontent.com/Mephistor666/SweetHomeMaidTranslate/main/Translation/sweethome-maid-cn.user.js)，
   Tampermonkey 会弹出安装页，点「安装」。或者在 Tampermonkey 里添加脚本，将sweethome-maid-cn.user.js的内容全部复制到添加的脚本中，然后点左上角的文件->保存。
3. 正常打开游戏玩。已翻译的剧情自动显示中文，第一次生效时右下角会提示 `汉化已生效：xxx`。
4. 关闭汉化在 Tampermonkey 里禁用脚本即可。

## 注意事项

- 只有**已翻译好并传到此库的剧情**是中文，其余仍是日文，游戏更新后需要等新剧情的译文更新上传后才有效。
- 译文从 GitHub 读取，需要能访问github。
- 看不到中文：先 `Ctrl+F5` 刷新（译文有 5 分钟缓存），再确认 Tampermonkey 里脚本处于启用状态。

## 实现

- 汉化方式参考了童话边境的翻译 [alex343425/otogitranslate](https://github.com/alex343425/otogitranslate)
- 翻译模型：[Sakura LLM](https://github.com/SakuraLLM/SakuraLLM) Sakura-Galtransl-14B-v3.8-Q4_K_M
- 主页点角色的对话气泡为普通翻译软件翻译。

## 声明

- 全部译文为**机器翻译**，仅供学习交流，请勿商用。
- Sakura LLM 系列模型为 CC BY-NC-SA 4.0 协议，禁止商用。
- 游戏版权归原厂所有。发现问题或有想补的翻译，欢迎加群交流，QQ：596809681。
