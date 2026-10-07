# 代码review助手

## 使用场景
给你一段 Python 代码，它找出潜在的空指针与资源泄漏。解决什么问题：人工 review 容易漏看边界。

## 输入 / 输出
输入一个 .py 文件路径，输出一份 Markdown 格式的 review 报告。

## 安装
```bash
pip install -r requirements.txt
```

## 环境要求
Python 3.9+，无需 GPU。依赖见 requirements.txt。

## 泛化
不限于 Python：把解析层换成 tree-sitter 的其它语言 grammar 即可适配 Java/Go。
