import pytest
PROVIDER = 'gmail_cbs_cleanroom_skill.'


def test_lazy_detail_replaces_list_and_back_preserves_folder(consumer):
    c = consumer
    c.act('v_mailboxes', {'id':'INBOX'})
    assert c.visible('v_messages') and not c.visible('v_message_detail')
    c.read('v_messages', {'ok':True,'items':[{'id':'m1','subject':'Synthetic'}]})
    assert all(name != PROVIDER+'portable_get_message' for name, _ in c.calls)
    c.act('v_messages', {'id':'m1'})
    assert c.visible('v_message_detail') and not c.visible('v_messages')
    c.read('v_message_detail', {'ok':True,'item':{'id':'m1','body':'Synthetic body'}})
    assert c.calls[-1] == (PROVIDER+'portable_get_message', {'id':'m1'})
    c.act('back-to-messages')
    assert c.state['selected_mailbox_id']=='INBOX'
    assert c.visible('v_messages') and not c.visible('v_message_detail')


def test_progressive_pages_and_query_folder_reset(consumer):
    c = consumer
    c.act('v_mailboxes', {'id':'INBOX'})
    reply={'ok':True,'items':[], 'nextPageToken':'page2', 'pagination':[{'id':'next','nextPageToken':'page2','summary':'Next page'}]}
    c.read('v_messages',reply)
    assert c.calls[-1][1]['page_token']==''
    assert c.widget('next-page')['dataSource']==c.widget('v_messages')['dataSource']
    c.act('next-page',reply['pagination'][0])
    c.read('v_messages',{'ok':True,'items':[],'pagination':[]})
    assert c.calls[-1][1]['page_token']=='page2'
    c.act('v_messages',{'id':'m2'})
    c.act('back-to-messages')
    assert c.state['page_token']=='page2'
    c.act('queries-v_messages',{'values':{'query':'receipt'}})
    assert c.state['page_token']=='' and c.state['query_q_search']=='receipt'
    c.state['page_token']='page3'
    c.act('v_mailboxes',{'id':'STARRED'})
    assert c.state['page_token']=='' and c.state['selected_message_id']==''


@pytest.mark.parametrize('widget,field,value,action',[
    ('e_toggle_read','isUnread',True,'mark_unread'),
    ('e_toggle_read','isUnread',False,'mark_read'),
    ('e_toggle_star','isStarred',True,'star'),
    ('e_toggle_star','isStarred',False,'unstar'),
])
def test_explicit_triage(consumer,widget,field,value,action):
    assert consumer.calls==[]
    assert consumer.act(widget,{'record':{'id':'m1'},'values':{field:value}},result={'ok':True})
    assert consumer.calls==[(PROVIDER+'portable_mutate_message',{'id':'m1','action':action})]


@pytest.mark.parametrize('failure',[
    {'ok':False,'error':'permission_denied'}, {'ok':False,'error':'account_not_connected'},
    {'ok':False,'error':'provider_unavailable','retryable':True},
    {'ok':False,'status':'pending'}, RuntimeError('offline'),
])
def test_failed_or_pending_archive_keeps_selection(consumer,failure):
    consumer.state.update(selected_mailbox_id='INBOX',selected_message_id='m1')
    assert not consumer.act('e_archive',{'record':{'id':'m1'}},result=failure)
    assert consumer.state['selected_message_id']=='m1'
    assert consumer.widget('e_archive')['inputs']['resetOnSuccess'] is False


def test_archive_confirmation_and_success(consumer):
    c=consumer
    c.state.update(selected_message_id='m1',selected_mailbox_id='INBOX')
    assert not c.act('e_archive',{'record':{'id':'m1'}},confirm=False)
    assert c.calls==[]
    assert c.act('e_archive',{'record':{'id':'m1'}},result={'ok':True})
    assert c.calls[-1][1]=={'id':'m1','action':'archive'}
    assert c.state['selected_message_id']=='' and c.state['selected_mailbox_id']=='INBOX'


def test_connection_discovery_does_not_attach(consumer):
    c=consumer
    c.read('v_connections',{'ok':True,'items':[{'id':'synthetic','email_address':'fixture@example.invalid','status':'available','attached':False}]})
    assert c.calls[-1]==(PROVIDER+'reusable_connections',{})
    c.act('v_connections',{'id':'synthetic'})
    assert len(c.calls)==1
    c.act('e_attach_connection',result={'ok':True,'attached':True})
    assert c.calls[-1]==(PROVIDER+'attach_reusable_connection',{'account_id':'synthetic'})


def test_independent_labels_and_visible_failure_fields(consumer):
    assert consumer.widget('v_mailboxes')['dataSource']['params']=={}
    fields=consumer.widget('message-read-status')['inputs']['fields']
    assert {'error','message'} <= {f['id'] for f in fields}
    assert consumer.widget('v_messages')['inputs']['emptyState']['title']
    assert consumer.widget('v_messages')['dataSource']['preserveLastValue'] is False
