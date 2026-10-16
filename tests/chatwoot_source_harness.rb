# Run the UNMODIFIED tagged listener, signer, presenter and assignment service,
# plus exact model methods extracted with Ruby's parser. Only Rails persistence,
# the dispatcher and HTTP I/O are test doubles. This is NOT a full Rails server.
require 'json'
require 'openssl'
require 'singleton'
require 'delegate'
require 'ostruct'
require 'prism'
require 'securerandom'

SOURCE = File.join(__dir__, 'fixtures', 'chatwoot-v4.18.0')
class Object
  def blank? = respond_to?(:empty?) ? empty? : !self
  def present? = !blank?
  def presence = present? ? self : nil
  def try(name, *args) = respond_to?(name) ? public_send(name, *args) : nil
end
class Class
  def prepend_mod_with(*) = nil
end
module Conversations; end
module Webhooks; end
module Messages
  module WebhookContentNormalizer
    def self.normalize(text) = text
  end
end
module SafeFetch
  class HttpError < StandardError; end
  def self.fetch(url, **options)
    $deliveries << {url: url, body: options[:body], headers: options[:headers]}
  end
end
module AgentBots
  class WebhookJob
    def self.perform_later(url, payload, kind, **options)
      Webhooks::Trigger.execute(url, payload, kind, **options)
    end
  end
end
module GlobalConfig
  def self.get_value(*) = nil
end
def Time.current = now
class FixtureContactInbox
  def hmac_verified = false
  def to_json(*) = {hmac_verified: false, source_id: 'fixture-source'}.to_json
end

load File.join(SOURCE, 'app_listeners_base_listener.rb')
load File.join(SOURCE, 'app_listeners_agent_bot_listener.rb')
load File.join(SOURCE, 'lib_webhooks_trigger.rb')
load File.join(SOURCE, 'app_services_conversations_assignment_service.rb')
load File.join(SOURCE, 'app_presenters_conversations_event_data_presenter.rb')

def upstream_methods(klass, file, names)
  source = File.read(File.join(SOURCE, file), encoding: 'utf-8')
  remaining = names.dup
  visit = lambda do |node|
    if node.is_a?(Prism::DefNode) && remaining.include?(node.name)
      klass.class_eval(source.lines[(node.location.start_line - 1)..(node.location.end_line - 1)].join,
                       file, node.location.start_line)
      remaining.delete(node.name)
    else
      node.compact_child_nodes.each { |child| visit.call(child) }
    end
  end
  visit.call(Prism.parse(source).value)
  raise "Missing upstream methods: #{remaining}" unless remaining.empty?
end

class Rows < Array
  def where(**) = self
  def chat = Rows.new(select { |row| [0, 1].include?(row.message_type) && !row.private })
  def count = length
end
class Actor
  attr_reader :id, :type
  def initialize(id, type)
    @id, @type = id, type
  end
  def push_event_data = {id: id, type: type}
  def webhook_data = type == 'contact' ? {id: id} : push_event_data
end
class User < Actor; end
class Bot < Actor
  attr_reader :outgoing_url, :secret
  def initialize(row)
    super(row.fetch('id'), 'agent_bot')
    @outgoing_url, @secret = row.fetch('outgoing_url'), row.fetch('secret')
  end
end
class AgentBot
  def self.accessible_to(*) = self
  def self.find_by(id:) = $bots.fetch(id)
end
class FixtureAccount
  def webhook_data = {id: 42}
end
class FixtureInbox
  attr_reader :id, :agent_bot
  def initialize(id, bot)
    @id, @agent_bot = id, bot
  end
  def active_bot? = true
  def agent_bot_inbox = OpenStruct.new(active?: true)
  def channel_type = 'Channel::WebWidget'
  def webhook_data = {id: id}
end
class FixtureConversation
  CONVERSATION_OPENED = :conversation_opened
  CONVERSATION_RESOLVED = :conversation_resolved
  CONVERSATION_STATUS_CHANGED = :conversation_status_changed
  CONVERSATION_READ = :conversation_read
  CONVERSATION_CONTACT_CHANGED = :conversation_contact_changed
  attr_accessor :status, :assignee, :ai_assignee, :waiting_since
  attr_reader :display_id, :inbox, :messages, :contact, :account, :updated_at, :created_at
  def initialize(id, inbox)
    @display_id, @inbox, @messages = id, inbox, Rows.new
    @contact, @account = Actor.new(id + 1000, 'contact'), FixtureAccount.new
    @status, @assignee, @ai_assignee = 'pending', nil, inbox.agent_bot
    @created_at = @updated_at = Time.now
    @saved_status = @status
    @saved_owner = [nil, @ai_assignee.id]
    @previous_changes = {}
  end
  def with_lock = yield
  def update(**values)
    values.each { |name, value| public_send("#{name}=", value) }
    save!
  end
  def save!
    old = @saved_status
    @status = @status.to_s
    @change = [old, status]
    @previous_changes = old == status ? {} : {'status' => @change}
    owner = [assignee&.id, ai_assignee&.id]
    if owner != @saved_owner
      @previous_changes['assignee_id'] = [@saved_owner[0], owner[0]]
      @previous_changes['assignee_agent_bot_id'] = [@saved_owner[1], owner[1]]
    end
    @saved_owner = owner
    @saved_status = status
    @updated_at = Time.now
    notify_status_change
    notify_conversation_updation
    self
  end
  def pending! = (@status = 'pending'; save!)
  def open! = (@status = 'open'; save!)
  def resolved! = (@status = 'resolved'; save!)
  def snoozed! = (@status = 'snoozed'; save!)
  def saved_change_to_status? = @change && @change[0] != @change[1]
  def saved_change_to_contact_last_seen_at? = false
  def saved_change_to_contact_id? = false
  # ActiveRecord after_commit has applied dirty changes; previous_changes is
  # retained, while status_change may be nil. Do not invent a webhook delta.
  def status_change = nil
  def previous_changes = @previous_changes
  def dispatch_conversation_updated_event(changes) = dispatcher_dispatch(:conversation_updated, changes)
  def dispatcher_dispatch(event, changes = nil)
    return unless AgentBotListener.instance.respond_to?(event)
    AgentBotListener.instance.public_send(event, OpenStruct.new(data: {conversation: self, changed_attributes: changes}))
  end
  def open? = status == 'open'
  def pending? = status == 'pending'
  def resolved? = status == 'resolved'
  def snoozed? = status == 'snoozed'
  def muted? = false
  def assignee_type = ai_assignee ? 'AgentBot' : (assignee ? 'User' : nil)
  def ai_assignee_type = ai_assignee ? 'AgentBot' : nil
  def assigned_entity = ai_assignee || assignee
  def webhook_data = Conversations::EventDataPresenter.new(self).webhook_data
  def push_data = Conversations::EventDataPresenter.new(self).push_data
  def can_reply? = true
  def contact_inbox = FixtureContactInbox.new
  def additional_attributes = {}
  def custom_attributes = {}
  def label_list = []
  def snoozed_until = nil
  def unread_incoming_messages = []
  def first_reply_created_at = nil
  def priority = nil
  def team = nil
  def agent_last_seen_at = 0
  def contact_last_seen_at = 0
  def last_activity_at = messages.last&.created_at || created_at
  def account_id = 42
  def inbox_id = inbox.id
end
upstream_methods(FixtureConversation, 'app_models_conversation.rb',
                 [:notify_status_change, :notify_conversation_updation, :allowed_keys?, :list_of_keys, :bot_handoff!])
class FixtureMessage
  attr_reader :id, :conversation, :sender, :content, :message_type, :private, :created_at, :content_attributes
  def initialize(conversation, body, actor)
    $message_id += 1
    @id, @conversation, @sender = $message_id, conversation, actor
    @content, @message_type = body.fetch('content'), body.fetch('message_type', 'incoming') == 'outgoing' ? 1 : 0
    @private, @content_attributes = body.fetch('private', false), body.fetch('content_attributes', {})
    @created_at = Time.now
  end
  def incoming? = message_type == 0
  def outgoing? = message_type == 1
  def account = conversation.account
  def inbox = conversation.inbox
  def webhook_sendable? = true
  def additional_attributes = {}
  def content_type = 'text'
  def webhook_content = content
  def processed_message_content = content
  def source_id = nil
  def attachments = []
  def push_event_data
    {id: id, content: content, message_type: message_type, private: private, content_type: content_type,
     sender: sender.push_event_data, content_attributes: content_attributes,
     created_at: created_at.to_i, conversation_id: conversation.display_id}
  end
  def create!
    conversation.messages << self
    reopen_conversation
    AgentBotListener.instance.message_created(OpenStruct.new(data: {message: self}))
    self
  end
end
upstream_methods(FixtureMessage, 'app_models_message.rb',
                 [:webhook_data, :webhook_push_event_data, :reopen_conversation, :reopen_resolved_conversation])

$bots, $conversations, $message_id = {}, {}, 0
STDOUT.sync = true
STDIN.set_encoding('utf-8')
STDOUT.set_encoding('utf-8')
STDIN.each_line do |line|
  $deliveries = []
  begin
    command = JSON.parse(line)
    command.fetch('bots', {}).each_value { |row| $bots[row.fetch('id')] = Bot.new(row) }
    cid = command.fetch('cid', 100)
    if command['op'] == 'visitor' && !$conversations.key?(cid)
      $conversations[cid] = FixtureConversation.new(cid, FixtureInbox.new(command.fetch('inbox', 7), $bots.fetch(command.fetch('bot'))))
    end
    conversation = $conversations[cid]
    response = case command.fetch('op')
               when 'visitor', 'human'
                 human = command['op'] == 'human'
                 actor = human ? User.new(99, 'user') : conversation.contact
                 body = {'content' => command.fetch('content'), 'message_type' => human ? 'outgoing' : 'incoming'}
                 if human
                   conversation.ai_assignee = nil
                   conversation.assignee = actor
                   conversation.open!
                 end
                 FixtureMessage.new(conversation, body, actor).create!.push_event_data
               when 'resolve'
                 conversation.resolved!
                 conversation.push_data
               when 'api'
                 part, method, body = command['part'], command['method'], command.fetch('body', {})
                 case part
                 when nil then conversation.push_data
                 when 'assignments'
                   actor = Conversations::AssignmentService.new(conversation: conversation, assignee_id: body['assignee_id'], assignee_type: body['assignee_type']).perform
                   actor&.push_event_data
                 when 'toggle_status'
                   if command['is_bot'] && body['status'] == 'open' && conversation.pending?
                     conversation.bot_handoff!(dispatch_event: false)
                   else
                     conversation.status = body.fetch('status'); conversation.save!
                   end
                   {payload: {status: conversation.status}}
                 when 'messages'
                   if method == 'POST'
                     FixtureMessage.new(conversation, body, $bots.fetch(command['bot'])).create!.push_event_data
                   else
                     params = command.fetch('params', {})
                     rows = conversation.messages
                     rows = rows.select { |r| r.id > params['after'].to_i }.first(100) if params['after']
                     rows = rows.select { |r| r.id < params['before'].to_i }.last(20) if params['before']
                     rows = rows.last(20) unless params['after'] || params['before']
                     {payload: rows.map(&:push_event_data)}
                   end
                 end
               end
    puts JSON.generate(response: response, deliveries: $deliveries)
  rescue Exception => error
    warn "#{error.class}: #{error.message}\n#{error.backtrace.first(6).join("\n")}"
    exit 1
  end
end
