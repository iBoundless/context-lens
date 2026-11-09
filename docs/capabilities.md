# Матрица возможностей

Матрица описывает проверенное поведение. Автоматические тесты используют локальные имитаторы и не отправляют запросы к моделям.

| Режим | Подтверждение | Состояние |
| --- | --- | --- |
| OpenCode 2.2.5 HTTP, ключ / подписка | SDK `@opencode/plugin@2.2.5`, `SessionHttpRequest.request`, `SessionHttpResponse.request/response` | Интерфейс и живой захват подтверждены |
| OpenCode 2.2.5 WebSocket | Тот же SDK, `experimental.ws.send/receive`, строковое поле `frame` | Живой захват подтверждён; экспериментальный, ограничения описаны в README |
| Codex 0.164.0 API-ключ | Custom provider: `base_url`, `env_key`, `wire_api=responses`, `supports_websockets` | Локальный HTTP/SSE/WS прокси проверен; реальный provider не подтверждён |
| Codex 0.164.0 подписка | Pre/PostModelRequest не обещают полный payload; TLS MITM исключён | Не подтверждено, отключено в первой версии |

Полнота захвата относится к клиентскому payload, а не к скрытому контексту провайдера. `previous_response_id` означает зависимость от серверного состояния; его нельзя автоматически считать полным контекстом. История сессий не подменяет перехват.

Источники:
- https://opencode.ai/v2/docs/build/plugins/server
- https://registry.npmjs.org/@opencode/plugin/-/plugin-2.2.5.tgz
- https://developers.openai.com/codex/hooks
- https://raw.githubusercontent.com/openai/codex/rust-v0.164.0/codex-rs/model-provider-info/src/lib.rs

Версии SDK и CLI указаны для воспроизводимости проверок. Поддержка Codex с подпиской не заявляется.
