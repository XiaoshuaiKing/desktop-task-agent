---
name: pytorch-training
description: 用于分析PyTorch模型训练不收敛、Loss异常、过拟合、欠拟合和训练效果异常问题。
---

# PyTorch训练问题分析

当用户询问PyTorch模型训练、Loss、准确率、收敛或过拟合问题时使用本Skill。

## 分析流程

1. 检查Dataset和标签是否正确
2. 检查输入数据shape和模型输入是否匹配
3. 检查模型输出shape和类别数量
4. 检查Loss函数是否正确
5. 检查梯度是否正常产生
6. 检查Optimizer是否更新参数
7. 检查Learning Rate是否合理
8. 比较Train和Validation结果
9. 判断是否存在过拟合或欠拟合
10. 给出最可能原因和修改建议

## 使用原则

- 优先根据用户真实代码和训练日志分析
- 不假设用户不存在的文件或模型
- 如果信息不足，明确指出缺少什么信息
- 修改代码时尽量保持用户原有工程结构
- 不为了优化而大范围重构

## References

需要更详细的训练排查知识时，读取：

- references/training_debug.md