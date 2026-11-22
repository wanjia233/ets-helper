# 第三方数据与许可说明

本仓库的**代码**是 MIT 许可。仓库里不含任何来自 E听说（讯飞）客户端的原始资源。

## 1. CMU Pronouncing Dictionary（`cmudict.dict`）

- 用途：音标反查的补充词库（约 13.5 万词，ARPAbet 格式，本程序运行时转成 IPA）。
- 来源：<https://github.com/cmusphinx/cmudict>
- 许可：BSD 2-Clause 风格许可，原文如下：

```
Copyright (C) 1993-2015 Carnegie Mellon University. All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice,
   this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY CARNEGIE MELLON UNIVERSITY ''AS IS'' AND ANY
EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL CARNEGIE MELLON UNIVERSITY BE LIABLE FOR ANY
DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF
THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## 2. 音标索引（`ipa_index.json` / `word_ipa.json`）——**不随仓库分发**

这两个文件是把「用户本机已安装的 E听说 客户端自带的词库」与 CMUdict 合并后生成的**衍生索引**，
体积数 MB，且内容属于第三方词库数据。因此：

- 它们被列入 `.gitignore`，不在仓库里；
- 程序首次运行时会在本机用 `python ets_helper.py --prebuild` 自动重建（约 3 秒）；
- 重建只读取你本机已有的数据，不上传任何东西。

## 3. 与 E听说 的关系

本项目与科大讯飞 / 广州讯飞易听说网络科技有限公司**没有任何关系**，非官方、未获授权。
程序只读取本机上已经存在的数据（客户端自己的缓存与日志），不修改、不破解、不上传。
使用时请自行确认符合你所在学校与平台的服务条款。
