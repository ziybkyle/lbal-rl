# 轨迹格式 v1

轨迹是一局一个 JSONL 文件。每行一个对象，第一行是头部，之后每旋一行，选择可以附在旋记录里。

## 头部

```json
{"type":"header","schema":"lbal-trace-v1","seed_text":"B2AG00000000","floor":20,"build":"16940935","data_sha256":"...","mods":["BetterLandlord 1.2.7"],"initial_deck":[...],"initial_items":[...]}
```

## 旋

```json
{"type":"spin","spin":1,"rent_due":25,"rents_paid":0,"coins":5,"earned":5,"spins_left":4,"tokens":{"reroll":0,"removal":0,"essence":0},"board":[...],"deck":[...],"items":[...],"candidates":[...],"chosen":{...},"events":[...],"outcome":"running"}
```

## 对比规则

- 头部字段必须一致。
- 同一旋的 `board`、`deck`、`items`、`candidates` 必须一致。
- `chosen` 按名字或编号一致。
- `events` 按顺序一致。
- 界面、动画、音效和纯装饰随机不进入对比。
