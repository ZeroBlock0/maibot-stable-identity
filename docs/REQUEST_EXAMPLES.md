# 合成请求处理前后

以下由当前宿主原始请求构造、Hook 参数整体替换及序列化/反序列化函数实际执行生成，数据全部为合成数据。使用 Hook 实际返回的 item_schema_version 校验，不使用默认值补齐。模型调用与聊天数据库为测试替身，未连接真实群聊。

## Planner 处理前

```text
<message msg_id="101" time="12:00:00" user="小明">
我在杭州工作
<message msg_id="102" time="12:00:00" user="小明">
我在成都工作
<message msg_id="103" time="12:00:00" user="改过的昵称">
我还是之前那个人
```

## Planner 处理后

```text
<message msg_id="101" time="12:00:00" user="小明" sender_id="qq:1001" person_id="1bc8b8291fd69e22a81527169ba22c7d">
我在杭州工作
<message msg_id="102" time="12:00:00" user="小明" sender_id="qq:1002" person_id="7d3126ec329362284fd782463d369985">
我在成都工作
<message msg_id="103" time="12:00:00" user="改过的昵称" sender_id="qq:1001">
我还是之前那个人
```

## Replyer 处理前

```text
<message msg_id="101" time="12:00:00" user="小明">
我在杭州工作
<message msg_id="102" time="12:00:00" user="小明">
我在成都工作
<message msg_id="103" time="12:00:00" user="改过的昵称">
我还是之前那个人
```

## Replyer 处理后

```text
<message msg_id="101" time="12:00:00" user="小明" sender_id="qq:1001" person_id="1bc8b8291fd69e22a81527169ba22c7d">
我在杭州工作
<message msg_id="102" time="12:00:00" user="小明" sender_id="qq:1002" person_id="7d3126ec329362284fd782463d369985">
我在成都工作
<message msg_id="103" time="12:00:00" user="改过的昵称" sender_id="qq:1001">
我还是之前那个人
```

完整 Context Items 见同目录 `model_requests.synthetic.json`；身份只加入临时请求，原始合成消息未改写。
