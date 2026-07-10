# Discord mention-path smoke test

The interactive `discord_bot` and send-only `discord_publisher` are separate services. A ready Discord gateway proves only that the bot logged in; it does not prove that a mention was authorized, queued to the `agent` worker, completed, and replied to.

## Required Discord configuration

In the Discord developer portal, enable the Message Content privileged intent. The bot role also needs these permissions in every allowed channel:

- View Channel and Read Message History
- Send Messages and Send Messages in Threads
- Create Public Threads (and Create Private Threads if private threads are used)
- Embed Links and Attach Files for chart responses

Keep guild, channel, and role allowlists configured. A gateway-ready bot with a mismatched allowlist will correctly reject the mention.

## Safe smoke procedure

1. Confirm the current `discord_bot` heartbeat, not heartbeat history:

   ```bash
   curl -sS -H "Authorization: Bearer $AGENT_API_BEARER_TOKEN" \
     "$AGENT_API_URL/runtime/heartbeats?service_role=discord_bot"
   ```

2. Verify `ready=true`, `message_content_intent=true`, and `runner=agent_worker_command_proxy`.

3. In an allowed channel, mention the bot with this non-trading prompt:

   > Reply with the service health summary only. Do not draft, approve, or place any trade.

4. Refresh the heartbeat and verify the `mention_path` fields advanced:

   - `last_message_seen_at_ms`
   - `last_authorized_mention_at_ms`
   - `last_command_id_enqueued`
   - `last_command_status=completed`
   - `last_reply_success_at_ms`

5. Inspect the command directly at `/commands/{last_command_id_enqueued}`. It must be an `ask` command owned by the `agent` role. This smoke test must not create a paper order, position, or live exchange action.

## Failure interpretation

| Evidence | Likely failure |
|---|---|
| `ready=false` | token, gateway, DNS, or Discord login failure |
| `message_content_intent=false` | privileged intent disabled in code or developer portal |
| no `last_message_seen_at_ms` | channel visibility/read permissions or gateway event delivery |
| message seen, `auth_rejection_count` rises | guild/channel/role allowlist mismatch |
| authorized mention, no command id | command enqueue/database failure; inspect `last_command_error` |
| command id remains enqueued/claimed | `agent` worker unavailable or command timeout |
| command completed, no reply success | thread/send permission failure; inspect `last_reply_error` and `last_thread_error` |
| `thread_fallback_count` rises | thread creation failed; the bot fell back to the source channel |

Use `/runtime/heartbeats/history?service_role=discord_bot` only for lifecycle forensics. Operational checks should use the current view.
