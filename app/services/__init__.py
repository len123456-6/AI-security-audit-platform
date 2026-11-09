"""业务逻辑层。

放"会被多个接口复用、且有真实计算/规则"的逻辑，例如：
  recipe_service        食谱 CRUD + 详情组装
  scaling_service       按 servings 缩放配料用量
  shopping_list_service 多道菜聚合为买菜清单（同类食材合并、常备品跳过）
  ai_service            调用大模型生成食谱结构（JSON 校验后落库）
路由层只负责取参、调 service、拼响应。
"""
