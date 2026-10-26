from .conftest import item_for, message


async def test_new_alias_collision_cannot_reuse_warm_verified_identity(host):
    first = message("SYNTHETIC_LONG_MESSAGE_053992", "1001")
    second = message("SYNTHETIC_LONG_MESSAGE_058070", "1002")
    host.messages = [first]
    plugin = await host.plugin()
    args = dict(item_schema_version=1, session_id="group-a")
    warm = await plugin.before_replyer(items=[item_for(first)], **args)
    assert 'identity_user_id="1001"' in str(warm)
    # Before the second message is persisted, its trusted inbound hint must
    # invalidate the warm answer without becoming evidence for the new sender.
    await plugin.observe_message(message=second)
    pending = await plugin.before_replyer(items=[item_for(second)], **args)
    assert 'identity_status="unknown"' in str(pending)
    assert 'identity_user_id="1001"' not in str(pending)
    host.messages.append(second)
    await plugin.observe_message(message=first)
    collision = await plugin.before_replyer(items=[item_for(first), item_for(second)], **args)
    assert str(collision).count('identity_status="unknown"') == 2
    assert 'identity_user_id="' not in str(collision)
