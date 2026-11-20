# PyTorch训练排查知识

## Loss不下降

优先检查：

1. 标签是否正确
2. 数据是否正确归一化
3. 模型输出是否与Loss要求匹配
4. 是否执行loss.backward()
5. 是否执行optimizer.step()
6. 学习率是否过大或过小

## 准确率不提升

检查：

- 类别分布是否严重不均衡
- 模型容量是否不足
- 输入特征是否有效
- Train和Validation预处理是否一致

## 过拟合

典型表现：

- Train Loss持续下降
- Train Accuracy持续提升
- Validation Loss上升
- Validation Accuracy不再提升

可以考虑：

- 数据增强
- Weight Decay
- Dropout
- Early Stopping
- 减小模型容量