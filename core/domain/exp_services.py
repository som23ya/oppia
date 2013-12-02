# coding: utf-8
#
# Copyright 2013 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Commands that can be used to operate on explorations.

All functions here should be agnostic of how ExplorationModel objects are
stored in the database. In particular, the various query methods should
delegate to the Exploration model class. This will enable the exploration
storage model to be changed without affecting this module and others above it.
"""

__author__ = 'Sean Lip'

import copy
import logging
import os
import StringIO
import zipfile

from core.domain import exp_domain
from core.domain import fs_domain
from core.domain import html_cleaner
from core.domain import obj_services
from core.domain import param_domain
from core.domain import rule_domain
from core.domain import stats_domain
from core.domain import value_generators_domain
from core.domain import widget_registry
from core.platform import models
import feconf
import jinja_utils
memcache_services = models.Registry.import_memcache_services()
transaction_services = models.Registry.import_transaction_services()
(exp_models,) = models.Registry.import_models([models.NAMES.exploration])
import utils


# TODO(sll): Unify this with the SUBMIT_HANDLER_NAMEs in other files.
SUBMIT_HANDLER_NAME = 'submit'
ADMIN_COMMITTER_ID = 'admin'
ALLOWED_CONTENT_TYPES = ['text']

# The current version of the exploration schema. If any backward-incompatible
# changes are made to the exploration schema in the YAML definitions, this
# version number must be changed and a migration process put in place.
CURRENT_EXPLORATION_SCHEMA_VERSION = 1


# Repository GET methods.
def _get_exploration_memcache_key(exploration_id):
    """Returns a memcache key for an exploration."""
    return 'exploration:%s' % exploration_id


def _get_state_memcache_key(exploration_id, state_id):
    """Returns a memcache key for a state."""
    return 'state:%s:%s' % (exploration_id, state_id)


def get_exploration_by_id(exploration_id, strict=True):
    """Returns a domain object representing an exploration."""
    exploration_memcache_key = _get_exploration_memcache_key(exploration_id)
    memcached_exploration = memcache_services.get_multi(
        [exploration_memcache_key]).get(exploration_memcache_key)

    if memcached_exploration is not None:
        return memcached_exploration
    else:
        exploration_model = exp_models.ExplorationModel.get(
            exploration_id, strict=strict)
        if exploration_model:
            exploration = exp_domain.Exploration(exploration_model)
            memcache_services.set_multi({
                exploration_memcache_key: exploration})
            return exploration
        else:
            return None


def get_state_by_id(exploration_id, state_id, strict=True):
    """Returns a domain object representing a state, given its id."""
    # TODO(sll): Generalize this to handle multiple state_ids at a time.
    state_memcache_key = _get_state_memcache_key(exploration_id, state_id)
    memcached_state = memcache_services.get_multi(
        [state_memcache_key]).get(state_memcache_key)

    if memcached_state is not None:
        return memcached_state
    else:
        state_model = exp_models.StateModel.get(
            exploration_id, state_id, strict=strict)
        if state_model:
            state = exp_domain.State.from_dict(state_id, state_model.value)
            memcache_services.set_multi({state_memcache_key: state})
            return state
        else:
            return None


# Query methods.
def get_all_explorations():
    """Returns a list of domain objects representing all explorations."""
    return [exp_domain.Exploration(e) for e in
            exp_models.ExplorationModel.get_all()]


def get_public_explorations():
    """Returns a list of domain objects representing public explorations."""
    return [exp_domain.Exploration(e) for e in
            exp_models.ExplorationModel.get_public_explorations()]


def get_viewable_explorations(user_id):
    """Returns domain objects for explorations viewable by the given user."""
    return [exp_domain.Exploration(e) for e in
            exp_models.ExplorationModel.get_viewable_explorations(user_id)]


def get_editable_explorations(user_id):
    """Returns domain objects for explorations editable by the given user."""
    return [e for e in get_viewable_explorations(user_id)
            if e.is_editable_by(user_id)]


def count_explorations():
    """Returns the total number of explorations."""
    return exp_models.ExplorationModel.get_exploration_count()


# Methods for exporting states and explorations to other formats.
def export_state_internals_to_dict(
        exploration_id, state_id, human_readable_dests=False):
    """Gets a Python dict of the internals of the state."""

    state = get_state_by_id(exploration_id, state_id)
    state_dict = copy.deepcopy(state.to_dict())

    if human_readable_dests:
        # Change the dest ids to human-readable names.
        for handler in state_dict['widget']['handlers']:
            for rule in handler['rule_specs']:
                if rule['dest'] != feconf.END_DEST:
                    dest_state = get_state_by_id(exploration_id, rule['dest'])
                    rule['dest'] = dest_state.name
    return state_dict


def export_state_to_dict(exploration_id, state_id):
    """Gets a Python dict representation of the state."""
    state_dict = export_state_internals_to_dict(exploration_id, state_id)
    state_dict.update({'id': state_id})
    return state_dict


def get_unresolved_answers_for_default_rule(exploration_id, state_id):
    """Gets the tally of unresolved answers that hit the default rule."""
    # TODO(sll): Add similar functionality for other rules? But then we have
    # to figure out what happens when those rules are edited/deleted.
    # TODO(sll): Should this return just the top N answers instead?
    return stats_domain.StateRuleAnswerLog.get(
        exploration_id, state_id, SUBMIT_HANDLER_NAME,
        exp_domain.DEFAULT_RULESPEC_STR).answers


def export_state_to_verbose_dict(exploration_id, state_id):
    """Gets a state dict with rule descriptions and unresolved answers."""
    exploration = get_exploration_by_id(exploration_id)

    state_dict = export_state_to_dict(exploration_id, state_id)

    state_dict['unresolved_answers'] = get_unresolved_answers_for_default_rule(
        exploration_id, state_id)

    # TODO(sll): Fix the frontend and remove this line.
    state_dict['widget']['id'] = state_dict['widget']['widget_id']

    for handler in state_dict['widget']['handlers']:
        for rule_spec in handler['rule_specs']:

            widget = widget_registry.Registry.get_widget_by_id(
                feconf.INTERACTIVE_PREFIX,
                state_dict['widget']['widget_id']
            )

            input_type = widget.get_handler_by_name(handler['name']).input_type

            rule_spec['description'] = rule_domain.get_rule_description(
                rule_spec['definition'], exploration.param_specs, input_type
            )

    return state_dict


def export_content_to_html(exploration_id, content_array, params=None):
    """Takes a Content array and transforms it into HTML.

    Args:
        exploration_id: the id of the exploration
        content_array: an array, each of whose members is of type Content. This
            object has two keys: type and value. Currently we expect the array
            to contain exactly one entry with type 'text'. The value is an
            HTML string.
        params: any parameters used for templatizing text strings.

    Returns:
        the HTML string representing the array.

    Raises:
        InvalidInputException: if content has no 'type' attribute, or an
            invalid 'type' attribute.
    """
    if params is None:
        params = {}

    html = ''
    for content in content_array:
        if content.type in ALLOWED_CONTENT_TYPES:
            value = jinja_utils.parse_string(content.value, params)

            html += '<div>%s</div>' % value
        else:
            raise utils.InvalidInputException(
                'Invalid content type %s', content.type)
    return html


def export_to_yaml(exploration_id):
    """Returns a YAML version of the exploration."""
    exploration = get_exploration_by_id(exploration_id)

    return utils.yaml_from_dict({
        'default_skin': exploration.default_skin,
        'param_changes': exploration.param_change_dicts,
        'param_specs': exploration.param_specs_dict,
        'states': [export_state_internals_to_dict(
            exploration_id, state_id, human_readable_dests=True)
            for state_id in exploration.state_ids],
        'schema_version': CURRENT_EXPLORATION_SCHEMA_VERSION
    })


def export_to_zip_file(exploration_id):
    """Returns a ZIP archive of the exploration."""
    yaml_repr = export_to_yaml(exploration_id)
    exploration = get_exploration_by_id(exploration_id)

    o = StringIO.StringIO()
    with zipfile.ZipFile(o, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('%s.yaml' % exploration.title, yaml_repr)

        fs = fs_domain.AbstractFileSystem(
            fs_domain.ExplorationFileSystem(exploration_id))
        dir_list = fs.listdir('')
        for filepath in dir_list:
            zf.writestr('assets/%s' % filepath, fs.get(filepath))

    return o.getvalue()


# Repository SAVE and DELETE methods.
def save_exploration(committer_id, exploration):
    """Commits an exploration domain object to persistent storage."""
    exploration.validate()

    def export_to_versionable_dict(exploration):
        """Returns a serialized version of this exploration for versioning.

        The criterion for whether an item is included in the return dict is:
        "suppose I am currently at v10 (say) and want to revert to v4; is this
        property something I would be happy with being overwritten?". Thus, the
        following properties are excluded for explorations:

            ['category', 'default_skin', 'editor_ids', 'is_public', 'title']

        The exploration id will be used to name the object in the history log,
        so it does not need to be saved within the returned dict.

        For states, all properties except 'id' are versioned. State dests are
        specified using names and not ids.
        """
        return {
            'param_changes': exploration.param_change_dicts,
            'param_specs': exploration.param_specs_dict,
            'states': [export_state_internals_to_dict(
                exploration.id, state_id, human_readable_dests=True)
                for state_id in exploration.state_ids]
        }

    def _save_exploration_transaction(committer_id, exploration):
        exploration_model = exp_models.ExplorationModel.get(exploration.id)
        if exploration.version != exploration_model.version:
            raise Exception(
                'Trying to update version %s of exploration from version %s, '
                'which is too old. Please reload the page and try again.'
                % (exploration_model.version, exploration.version))

        exploration_memcache_key = _get_exploration_memcache_key(
            exploration.id)
        memcache_services.delete(exploration_memcache_key)
        
        properties_dict = {
            'category': exploration.category,
            'title': exploration.title,
            'state_ids': exploration.state_ids,
            'param_specs': exploration.param_specs_dict,
            'param_changes': exploration.param_change_dicts,
            'is_public': exploration.is_public,
            'editor_ids': exploration.editor_ids,
            'default_skin': exploration.default_skin,
            'version': exploration_model.version,
        }

        versionable_dict = feconf.NULL_SNAPSHOT
        if exploration.is_public:
            versionable_dict = export_to_versionable_dict(exploration)

        # Create a snapshot for the version history.
        exploration_model.put(
            committer_id, properties_dict, snapshot=versionable_dict)

    transaction_services.run_in_transaction(
        _save_exploration_transaction, committer_id, exploration)


def save_states(committer_id, exploration_id, states):
    """Commits state domain objects to persistent storage.

    It is the caller's responsibility to commit the exploration, if
    appropriate. For safety, calls to save_states() should be in a transaction
    with calls to save_exploration() (or with datastore operations on the
    corresponding Explorations).
    """
    # TODO(sll): This should probably be refactored as follows: the exploration
    # domain object would store a list that accumulates actions to perform
    # when the exploration domain object is saved. This method would then not
    # exist, so it cannot be called independently of save_exploration().
    state_memcache_keys = [_get_state_memcache_key(exploration_id, state.id)
                           for state in states]
    memcache_services.delete_multi(state_memcache_keys)

    for state in states:
        state.validate()

    def _save_states_transaction(committer_id, exploration_id, states):
        state_ids = [state.id for state in states]
        state_models = exp_models.StateModel.get_multi(
            exploration_id, state_ids, strict=False)

        for ind, state_model in enumerate(state_models):
            # Craete a new state if it does not already exist.
            if state_model is None:
                state_models[ind] = exp_models.StateModel(
                    id=states[ind].id, exploration_id=exploration_id)
            state_models[ind].value = states[ind].to_dict()

        exp_models.StateModel.put_multi(state_models)

    transaction_services.run_in_transaction(
        _save_states_transaction, committer_id, exploration_id, states)


def create_new(
    user_id, title, category, exploration_id=None,
        init_state_name=feconf.DEFAULT_STATE_NAME):
    """Creates and saves a new exploration; returns its id."""
    # Generate a new exploration id, if one wasn't passed in.
    exploration_id = (exploration_id or
                      exp_models.ExplorationModel.get_new_id(title))

    state_id = exp_models.StateModel.get_new_id(init_state_name)
    new_state = exp_domain.State(
        state_id, init_state_name, [exp_domain.Content('text', '')], [], None)
    save_states(user_id, exploration_id, [new_state])

    exploration_model = exp_models.ExplorationModel(
        id=exploration_id, title=title, category=category,
        state_ids=[state_id], editor_ids=[user_id])
    exploration_model.put(user_id, {})

    return exploration_model.id


def delete_state_model(exploration_id, state_id, force_deletion=False):
    """Marks a state model as deleted and removes it from memcache.

    IMPORTANT: Callers of this function should ensure that committer_id has
    permissions to delete this state, prior to calling this function.

    If force_deletion is True the state is fully deleted and is unrecoverable.
    Otherwise, the state is marked as deleted, but the corresponding model is
    still retained in the datastore. This last option is the preferred one.
    """
    state_model = exp_models.StateModel.get(exploration_id, state_id)
    if force_deletion:
        state_model.delete()
    else:
        state_model.deleted = True
        state_model.put()

    state_memcache_key = _get_state_memcache_key(exploration_id, state_id)
    memcache_services.delete(state_memcache_key)


def delete_exploration(committer_id, exploration_id, force_deletion=False):
    """Deletes the exploration with the given exploration_id.

    IMPORTANT: Callers of this function should ensure that committer_id has
    permissions to delete this exploration, prior to calling this function.

    If force_deletion is True the exploration and its history are fully deleted
    and are unrecoverable. Otherwise, the exploration and all its history are
    marked as deleted, but the corresponding models are still retained in the
    datastore. This last option is the preferred one.
    """
    exploration = get_exploration_by_id(exploration_id)

    # This must come after the exploration is retrieved. Otherwise the memcache
    # key will be reinstated.
    exploration_memcache_key = _get_exploration_memcache_key(exploration_id)
    memcache_services.delete(exploration_memcache_key)

    for state_id in exploration.state_ids:
        delete_state_model(
            exploration_id, state_id, force_deletion=force_deletion)

    exploration_model = exp_models.ExplorationModel.get(exploration_id)
    if force_deletion:
        exploration_model.delete()
    else:
        exploration_model.put(committer_id, {'deleted': True})

    for snapshot in exp_models.ExplorationSnapshotModel.get_all():
        if snapshot.exploration_id == exploration_id:
            if force_deletion:
                snapshot.delete()
            else:
                snapshot.deleted = True
                snapshot.put()

    for snapshot in exp_models.ExplorationSnapshotContentModel.get_all():
        if snapshot.exploration_id == exploration_id:
            if force_deletion:
                snapshot.delete()
            else:
                snapshot.deleted = True
                snapshot.put()


# Operations involving exploration parameters.
def get_init_params(exploration_id):
    """Returns an initial set of exploration parameters for a reader."""
    exploration = get_exploration_by_id(exploration_id)

    # Note that the list of parameter changes is ordered. Parameter changes
    # later in the list may depend on parameter changes that have been set
    # earlier in the same list.
    new_params = {}
    for pc in exploration.param_changes:
        obj_type = exploration.get_obj_type_for_param(pc.name)
        new_params[pc.name] = pc.get_normalized_value(obj_type, new_params)
    return new_params


def update_with_state_params(exploration_id, state_id, reader_params):
    """Updates a reader's params using the params for the given state."""
    exploration = get_exploration_by_id(exploration_id)
    state = get_state_by_id(exploration_id, state_id)
    new_params = copy.deepcopy(reader_params)

    # Note that the list of parameter changes is ordered. Parameter changes
    # later in the list may depend on parameter changes that have been set
    # earlier in the same list.
    for pc in state.param_changes:
        obj_type = exploration.get_obj_type_for_param(pc.name)
        new_params[pc.name] = pc.get_normalized_value(obj_type, new_params)
    return new_params


# Operations on exploration snapshots.
def get_exploration_snapshots_metadata(exploration_id, limit):
    """Returns the most recent snapshots for this exploration, as dicts.

    Args:
        exploration_id: str. The id of the exploration in question.
        limit: int. The maximum number of snapshots to return.

    Returns:
        list of dicts, each representing a recent snapshot. Each dict has the
        following keys: committer_id, commit_message, created_on,
        version_number. The version numbers are consecutive and in descending
        order. There are max(limit, exploration.version_number) items in the
        returned list.
    """
    exploration = get_exploration_by_id(exploration_id)
    oldest_version = max(exploration.version - limit, 0) + 1
    current_version = exploration.version
    version_nums = range(current_version, oldest_version - 1, -1)

    return [exp_models.ExplorationSnapshotModel.get_metadata(
        exploration_id, version_num
    ) for version_num in version_nums]


# Operations on states belonging to an exploration.
def add_states(committer_id, exploration_id, state_names):
    """Adds multiple states at a time. Commits changes.

    Returns the corresponding list of state_ids.
    """
    exploration = get_exploration_by_id(exploration_id)
    for state_name in state_names:
        if exploration.has_state_named(state_name):
            raise ValueError('Duplicate state name %s' % state_name)

    state_ids = [exp_models.StateModel.get_new_id(name) for name in state_names]
    new_states = []
    for ind, state_id in enumerate(state_ids):
        new_states.append(exp_domain.State(
            state_id, state_names[ind], [exp_domain.Content('text', '')],
            [], None))

    def _add_states_transaction(committer_id, exploration_id, new_states):
        exploration_memcache_key = _get_exploration_memcache_key(
            exploration_id)
        memcache_services.delete(exploration_memcache_key)

        save_states(committer_id, exploration_id, new_states)
        exploration = get_exploration_by_id(exploration_id)
        exploration.state_ids += state_ids
        save_exploration(committer_id, exploration)

    transaction_services.run_in_transaction(
        _add_states_transaction, committer_id, exploration_id, new_states)

    return state_ids


def update_state(committer_id, exploration_id, state_id, new_state_name,
                 param_changes, widget_id, widget_customization_args,
                 widget_handlers, widget_sticky, content):
    """Updates the given state, and commits changes.

    Args:
    - committer_id: str. Email address of the user who is performing the update
        action.
    - exploration_id: str. The id of the exploration.
    - state_id: str. The id of the state being updated.
    - new_state_name: str or None. If present, the new name for the state.
    - param_changes: list of dicts with keys ('name', 'generator_id',
        'customization_args'), or None. If present, represents parameter
        changes that should be applied when a reader enters the state.
    - widget_id: str or None. If present, the id of the interactive widget for
        this state.
    - widget_customization_args: dict or None. If present, the
        customization_args used to render the interactive widget for this
        state.
    - widget_handlers: dict or None. If present, it represents the handler and
        rule specifications for this state.
    - widget_sticky: bool or None. If present, the setting for whether the
        interactive widget for this state should be preserved when the reader
        navigates to another state that uses the same interactive widget. For
        example, we might want a textarea containing user-entered code to
        retain that code in a state transition, rather than being overwritten
        with a brand-new textarea.
    - content: None, or a list of dicts, where each dict has keys ('type',
        'value'). Currently we expect this list to have exactly one element
        with type 'text'. If present, this list represents the non-interactive
        content for the state.
    """
    # TODO(sll): Add more documentation for widget_handlers, above.

    exploration = get_exploration_by_id(exploration_id)
    state = get_state_by_id(exploration_id, state_id)

    if new_state_name:
        if (state.name != new_state_name and
                exploration.has_state_named(new_state_name)):
            raise ValueError('Duplicate state name: %s' % new_state_name)
        state.name = new_state_name

    if param_changes:
        if not isinstance(param_changes, list):
            raise Exception(
                'Expected param_changes to be a list, received %s' % 
                param_changes)
        state.param_changes = []
        for param_change in param_changes:
            if not isinstance(param_change, dict):
                raise Exception(
                    'Expected element of param_changes to be a dictionary, '
                    'received %s' % param_change)
            exp_param_spec = exploration.param_specs.get(param_change['name'])
            if exp_param_spec is None:
                raise Exception('No parameter named %s exists in this '
                                'exploration' % param_change['name'])

            # TODO(sll): Here (or when validating the state before committing),
            # check whether some sample generated values match the expected
            # obj_type.

            state.param_changes.append(param_domain.ParamChange(
                param_change['name'], param_change['generator_id'],
                param_change['customization_args']))

    if widget_id:
        state.widget.widget_id = widget_id

    if widget_customization_args is not None:
        if not isinstance(widget_customization_args, dict):
            raise Exception(
                'Expected widget_customization_args to be a dictionary, '
                'received %s' % widget_customization_args)
        state.widget.customization_args = widget_customization_args

    if widget_sticky is not None:
        if not isinstance(widget_sticky, bool):
            raise Exception(
                'Expected widget_sticky to be a boolean, received %s' %
                widget_sticky)
        state.widget.sticky = widget_sticky

    if widget_handlers:
        if not isinstance(widget_handlers, dict):
            raise Exception(
                'Expected widget_handlers to be a dictionary, received %s' 
                % widget_handlers)
        ruleset = widget_handlers['submit']
        if not isinstance(ruleset, list):
            raise Exception(
                'Expected widget_handlers[submit] to be a list, received %s'
                % ruleset)
        utils.recursively_remove_key(ruleset, u'$$hashKey')

        state.widget.handlers = [
            exp_domain.AnswerHandlerInstance('submit', [])]

        generic_widget = widget_registry.Registry.get_widget_by_id(
            'interactive', state.widget.widget_id)

        # TODO(yanamal): Do additional calculations here to get the
        # parameter changes, if necessary.
        for rule_ind in range(len(ruleset)):
            rule = ruleset[rule_ind]

            if not isinstance(rule, dict):
                raise Exception(
                    'Expected rule to be a dictionary, received %s' % rule)
            if not isinstance(rule['definition'], dict):
                raise Exception(
                    'Expected rule[\'definition\'] to be a dictionary, '
                    'received %s' % rule['definition'])
            if not isinstance(rule['feedback'], list):
                raise Exception(
                    'Expected rule[\'feedback\'] to be a list, received %s'
                    % rule['feedback'])

            if rule.get('dest') not in ([feconf.END_DEST] + exploration.state_ids):
                raise ValueError(
                    'The destination %s is not a valid state id'
                    % rule.get('dest'))

            state_rule = exp_domain.RuleSpec(
                rule.get('definition'), rule.get('dest'),
                [html_cleaner.clean(feedback) for feedback
                                              in rule.get('feedback')],
                rule.get('param_changes'))

            if rule['description'] == feconf.DEFAULT_RULE_NAME:
                if rule_ind != len(ruleset) - 1:
                    raise ValueError(
                        'Invalid ruleset: rules other than the ' 
                        'last one should not be default rules.')
                if (rule['definition']['rule_type'] != 
                        rule_domain.DEFAULT_RULE_TYPE):
                    raise ValueError(
                        'For a default rule the rule_type should be %s not %s'
                        % rule_domain.DEFAULT_RULE_TYPE
                        % rule['definition']['rule_type'])           
            else:
                if rule_ind == len(ruleset) - 1:
                    raise ValueError(
                        'Invalid ruleset: the last rule should be a default '
                        'rule')
                if (rule['definition']['rule_type'] ==
                        rule_domain.DEFAULT_RULE_TYPE):
                    raise ValueError(
                        'For a non-default rule the rule_type should not be %s'
                        % rule_domain.DEFAULT_RULE_TYPE)

                # TODO(sll): Generalize this to Boolean combinations of rules.
                matched_rule = generic_widget.get_rule_by_name(
                    'submit', state_rule.definition['name'])

                # Normalize and store the rule params.
                # TODO(sll): Generalize this to Boolean combinations of rules.
                rule_inputs = state_rule.definition['inputs']
                if not isinstance(rule_inputs, dict):
                    raise Exception(
                        'Expected rule_inputs to be a dict, received %s'
                        % rule_inputs)
                for param_name, value in rule_inputs.iteritems():
                    param_type = rule_domain.get_obj_type_for_param_name(
                        matched_rule, param_name)

                    if (isinstance(value, basestring) and 
                            '{{' in value and '}}' in value):
                        # TODO(jacobdavis11): Create checks that all parameters
                        # referred to exist and have the correct types
                        normalized_param = value
                    else:
                        try:
                            normalized_param = param_type.normalize(value)
                        except TypeError:
                            raise Exception('%s has the wrong type. '
                                'Please replace it with a %s.' %
                                 (value, param_type.__name__))
                    rule_inputs[param_name] = normalized_param     

            state.widget.handlers[0].rule_specs.append(state_rule)

    if content:
        if not isinstance(content, list):
            raise Exception(
                'Expected content to be a list, received %s' % content)
        if len(content) != 1:
            raise Exception(
                'Expected content to have length 1, received %s' % content)
        if not isinstance(content[0], dict):
            raise Exception(
                'Expected entry in content to be a dict, received %s' 
                % content[0])
    
        # TODO(sll): Must sanitize all content in noninteractive widget attrs.
        state.content = [exp_domain.Content(
            content[0]['type'], html_cleaner.clean(content[0]['value']))]

    def _update_state_transaction(committer_id, exploration, state):
        save_states(committer_id, exploration.id, [state])
        save_exploration(committer_id, exploration)

    transaction_services.run_in_transaction(
        _update_state_transaction, committer_id, exploration, state)


def delete_state(committer_id, exploration_id, state_id):
    """Deletes the given state. Commits changes."""
    exploration = get_exploration_by_id(exploration_id)
    if state_id not in exploration.state_ids:
        raise ValueError('Invalid state id %s for exploration %s' %
                         (state_id, exploration.id))

    # Do not allow deletion of initial states.
    if exploration.state_ids[0] == state_id:
        raise ValueError('Cannot delete initial state of an exploration.')

    def _delete_state_transaction(committer_id, exploration_id, state_id):
        exploration = get_exploration_by_id(exploration_id)

        # Find all destinations in the exploration which equal the deleted
        # state, and change them to loop back to their containing state.
        for other_state_id in exploration.state_ids:
            other_state = get_state_by_id(exploration_id, other_state_id)
            changed = False
            for handler in other_state.widget.handlers:
                for rule in handler.rule_specs:
                    if rule.dest == state_id:
                        rule.dest = other_state_id
                        changed = True
            if changed:
                save_states(committer_id, exploration_id, [other_state])

        # Delete the state with id state_id.
        exploration_memcache_key = _get_exploration_memcache_key(
            exploration_id)
        memcache_services.delete(exploration_memcache_key)

        delete_state_model(exploration_id, state_id)
        exploration.state_ids.remove(state_id)
        save_exploration(committer_id, exploration)

    transaction_services.run_in_transaction(
        _delete_state_transaction, committer_id, exploration_id, state_id)


def classify(exploration_id, state_id, handler_name, answer, params):
    """Return the first rule that is satisfied by a reader's answer."""

    exploration = get_exploration_by_id(exploration_id)
    state = get_state_by_id(exploration_id, state_id)

    # Get the widget to determine the input type.
    generic_handler = widget_registry.Registry.get_widget_by_id(
        feconf.INTERACTIVE_PREFIX, state.widget.widget_id
    ).get_handler_by_name(handler_name)

    handler = next(h for h in state.widget.handlers if h.name == handler_name)
    fs = fs_domain.AbstractFileSystem(
        fs_domain.ExplorationFileSystem(exploration_id))

    if generic_handler.input_type is None:
        return handler.rule_specs[0]
    else:
        for rule_spec in handler.rule_specs:
            if rule_domain.evaluate_rule(
                    rule_spec.definition, exploration.param_specs,
                    generic_handler.input_type, params, answer, fs):
                return rule_spec

        raise Exception(
            'No matching rule found for handler %s.' % handler.name)


# Creation and deletion methods.
def create_from_yaml(
    yaml_content, user_id, title, category, exploration_id=None):
    """Creates an exploration from a YAML text string."""
    exploration_dict = utils.dict_from_yaml(yaml_content)

    exploration_schema_version = exploration_dict.get('schema_version')

    if exploration_schema_version != CURRENT_EXPLORATION_SCHEMA_VERSION:
        raise Exception('Sorry, we can only process v1 YAML files at present.')

    state_names_to_ids = {}

    init_state_name = exploration_dict['states'][0]['name']

    exploration_id = create_new(
        user_id, title, category, exploration_id=exploration_id,
        init_state_name=init_state_name)

    exploration = get_exploration_by_id(exploration_id)
    state_names_to_ids[init_state_name] = exploration.state_ids[0]

    try:
        exploration_param_specs = {
            ps_name: param_domain.ParamSpec.from_dict(ps_val)
            for (ps_name, ps_val) in exploration_dict['param_specs'].iteritems()
        }

        other_state_names = [
            sdict['name'] for sdict in exploration_dict['states']
            if sdict['name'] != init_state_name]
        other_state_ids = add_states(
            user_id, exploration_id, other_state_names)

        for ind, other_state_id in enumerate(other_state_ids):
            state_names_to_ids[other_state_names[ind]] = other_state_id

        all_states = []

        for sdict in exploration_dict['states']:
            state = get_state_by_id(
                exploration_id, state_names_to_ids[sdict['name']])

            state.content = [
                exp_domain.Content(
                    item['type'], html_cleaner.clean(item['value']))
                for item in sdict['content']
            ]

            state.param_changes = [param_domain.ParamChange(
                pc['name'], pc['generator_id'], pc['customization_args']
            ) for pc in sdict['param_changes']]

            for pc in state.param_changes:
                if pc.name not in exploration_param_specs:
                    raise Exception('Parameter %s was used in a state but not '
                                    'declared in the exploration param_specs.'
                                    % pc.name)

            wdict = sdict['widget']
            widget_handlers = [exp_domain.AnswerHandlerInstance.from_dict({
                'name': handler['name'],
                'rule_specs': [{
                    'definition': rule_spec['definition'],
                    'dest': (
                        feconf.END_DEST if rule_spec['dest'] == feconf.END_DEST
                        else state_names_to_ids[rule_spec['dest']]
                    ),
                    'feedback': [html_cleaner.clean(feedback)
                                 for feedback in rule_spec['feedback']],
                    'param_changes': rule_spec.get('param_changes', []),
                } for rule_spec in handler['rule_specs']],
            }) for handler in wdict['handlers']]

            state.widget = exp_domain.WidgetInstance(
                wdict['widget_id'], wdict['customization_args'],
                widget_handlers, wdict['sticky'])

            all_states.append(state)

        save_states(user_id, exploration_id, all_states)

        exploration = get_exploration_by_id(exploration_id)
        exploration.default_skin = exploration_dict['default_skin']
        exploration.param_changes = [param_domain.ParamChange(
            pc['name'], pc['generator_id'], pc['customization_args']
        ) for pc in exploration_dict['param_changes']]
        exploration.param_specs = exploration_param_specs
        save_exploration(user_id, exploration)
    except Exception:
        delete_exploration(user_id, exploration_id, force_deletion=True)
        raise

    return exploration_id


def fork_exploration(old_exploration_id, user_id):
    """Forks an exploration and returns the new exploration's id."""
    old_exploration = get_exploration_by_id(old_exploration_id)
    if not old_exploration.is_forkable_by(user_id):
        raise Exception('You cannot copy this exploration.')

    new_exploration_id = create_from_yaml(
        export_to_yaml(old_exploration_id), user_id,
        'Copy of %s' % old_exploration.title, old_exploration.category
    )

    # Duplicate the assets of the old exploration.
    old_fs = fs_domain.AbstractFileSystem(
        fs_domain.ExplorationFileSystem(old_exploration_id))
    new_fs = fs_domain.AbstractFileSystem(
        fs_domain.ExplorationFileSystem(new_exploration_id))

    dir_list = old_fs.listdir('')
    for filepath in dir_list:
        file_content = old_fs.get(filepath)
        new_fs.put(filepath, file_content)

    return new_exploration_id


def get_demo_exploration_components(demo_path):
    """Gets the content of `demo_path` in the sample explorations folder.

    Args:
      demo_path: the file or folder path for the content of an exploration
        in SAMPLE_EXPLORATIONS_DIR. E.g.: 'adventure.yaml' or 'tar/'.

    Returns:
      a 2-tuple, the first element of which is a yaml string, and the second
      element of which is a list of (filepath, content) 2-tuples. The filepath
      does not include the assets/ prefix.
    """
    demo_filepath = os.path.join(feconf.SAMPLE_EXPLORATIONS_DIR, demo_path)

    if demo_filepath.endswith('yaml'):
        file_contents = utils.get_file_contents(demo_filepath)
        return file_contents, []
    elif os.path.isdir(demo_filepath):
        return utils.get_exploration_components_from_dir(demo_filepath)
    else:
        raise Exception('Unrecognized file path: %s' % demo_path)


def load_demo(exploration_id):
    """Loads a demo exploration."""
    # TODO(sll): Speed this method up. It is too slow.

    if not (0 <= int(exploration_id) < len(feconf.DEMO_EXPLORATIONS)):
        raise Exception('Invalid demo exploration id %s' % exploration_id)

    exploration = feconf.DEMO_EXPLORATIONS[int(exploration_id)]

    if len(exploration) == 3:
        (exp_filename, title, category) = exploration
    else:
        raise Exception('Invalid demo exploration: %s' % exploration)

    yaml_content, assets_list = get_demo_exploration_components(exp_filename)
    exploration_id = create_from_yaml(
        yaml_content, ADMIN_COMMITTER_ID, title, category,
        exploration_id=exploration_id)

    for (asset_filename, asset_content) in assets_list:
        fs = fs_domain.AbstractFileSystem(
            fs_domain.ExplorationFileSystem(exploration_id))
        fs.put(asset_filename, asset_content)

    exploration = get_exploration_by_id(exploration_id)
    exploration.is_public = True
    save_exploration(ADMIN_COMMITTER_ID, exploration)

    logging.info('Exploration with id %s was loaded.' % exploration_id)


def delete_demo(exploration_id):
    """Deletes a single demo exploration."""
    exploration = get_exploration_by_id(exploration_id, strict=False)
    if not exploration:
        # This exploration does not exist, so it cannot be deleted.
        logging.info('Exploration with id %s was not deleted, because it '
                     'does not exist.' % exploration_id)
    else:
        delete_exploration(
            ADMIN_COMMITTER_ID, exploration_id, force_deletion=True)


def load_demos():
    """Initializes the demo explorations."""
    for index in range(len(feconf.DEMO_EXPLORATIONS)):
        load_demo(str(index))


def delete_demos():
    """Deletes the demo explorations."""
    for index in range(len(feconf.DEMO_EXPLORATIONS)):
        delete_demo(str(index))


def reload_demos():
    """Reloads the demo explorations."""
    delete_demos()
    load_demos()


# Verification methods.
def verify_state_dict(state_dict, state_name_list, exp_param_specs_dict):
    """Verifies a state dictionary that came from a YAML file."""

    def _verify_content(state_content_list):
        """Checks that a state content list specification is valid."""
        CONTENT_ITEM_SCHEMA = [
            ('type', basestring), ('value', basestring)]

        if len(state_content_list) != 1:
            raise Exception(
                'Each state content list should contain exactly one element. %s'
                % state_content_list)

        for content_item in state_content_list:
            utils.verify_dict_keys_and_types(content_item, CONTENT_ITEM_SCHEMA)
            if content_item['type'] not in ALLOWED_CONTENT_TYPES:
                raise Exception('Unsupported content type %s.' %
                                content_item['type'])

    def _verify_param_changes(param_changes, exp_param_specs_dict):
        """Checks that a param_changes specification is valid."""

        PARAM_CHANGE_SCHEMA = [
            ('name', basestring), ('generator_id', basestring),
            ('customization_args', utils.ANY_TYPE)]

        generator_registry = value_generators_domain.Registry

        for pc in param_changes:
            utils.verify_dict_keys_and_types(pc, PARAM_CHANGE_SCHEMA)
            if pc['name'] not in exp_param_specs_dict:
                raise Exception('Undeclared param name: %s' % pc['name'])

            # Check that the generator id exists.
            generator_registry.get_generator_class_by_id(pc['generator_id'])

            for arg_name in pc['customization_args']:
                if not isinstance(arg_name, basestring):
                    raise Exception('Invalid param change customization arg '
                                    'name: %s' % arg_name)

        # TODO(sll): Find a way to verify the customization args when they
        # depend on context parameters. Can we get sample values for the
        # reader's answer and these parameters by looking at states that
        # link to this one?

    ATOMIC_RULE_DEFINITION_SCHEMA = [
        ('inputs', dict), ('name', basestring), ('rule_type', basestring),
        ('subject', basestring)]
    COMPOSITE_RULE_DEFINITION_SCHEMA = [
       ('children', list), ('rule_type', basestring)]
    DEFAULT_RULE_DEFINITION_SCHEMA = [('rule_type', basestring)]
    ALLOWED_COMPOSITE_RULE_TYPES = [
        rule_domain.AND_RULE_TYPE, rule_domain.OR_RULE_TYPE,
        rule_domain.NOT_RULE_TYPE]

    def _verify_rule_definition(rule_definition, exp_param_specs_dict):
        """Verify a rule definition."""

        if 'rule_type' not in rule_definition:
            raise Exception('Rule definition %s contains no rule type.'
                            % rule_definition)

        rule_type = rule_definition['rule_type']

        if rule_type == rule_domain.DEFAULT_RULE_TYPE:
            utils.verify_dict_keys_and_types(
                rule_definition, DEFAULT_RULE_DEFINITION_SCHEMA)
        elif rule_type  == rule_domain.ATOMIC_RULE_TYPE:
            utils.verify_dict_keys_and_types(
                rule_definition, ATOMIC_RULE_DEFINITION_SCHEMA)

            if (rule_definition['subject'] not in exp_param_specs_dict
                    and rule_definition['subject'] != 'answer'):
                raise Exception('Unrecognized rule subject: %s' %
                                rule_definition['subject'])
        else:
            if rule_type not in ALLOWED_COMPOSITE_RULE_TYPES:
                raise Exception('Unsupported rule type %s.' % rule_type)

            utils.verify_dict_keys_and_types(
                rule_definition, COMPOSITE_RULE_DEFINITION_SCHEMA)
            for child_rule in rule_definition['children']:
                _verify_rule_definition(child_rule, exp_param_specs_dict)

    STATE_DICT_SCHEMA = [
        ('content', list), ('name', basestring), ('param_changes', list),
        ('widget', dict)]
    WIDGET_SCHEMA = [
        ('widget_id', basestring), ('customization_args', dict),
        ('handlers', list), ('sticky', bool)]
    HANDLER_SCHEMA = [('name', basestring), ('rule_specs', list)]
    RULE_SCHEMA = [
        ('definition', dict), ('dest', basestring), ('feedback', list),
        ('param_changes', list)]

    utils.verify_dict_keys_and_types(state_dict, STATE_DICT_SCHEMA)
    _verify_content(state_dict['content'])
    _verify_param_changes(state_dict['param_changes'], exp_param_specs_dict)
    utils.verify_dict_keys_and_types(state_dict['widget'], WIDGET_SCHEMA)

    curr_state_name = state_dict['name']

    for handler in state_dict['widget']['handlers']:
        utils.verify_dict_keys_and_types(handler, HANDLER_SCHEMA)

        if not handler['rule_specs']:
            raise Exception('There must be at least one rule.')

        for rule in handler['rule_specs']:
            utils.verify_dict_keys_and_types(rule, RULE_SCHEMA)

            _verify_rule_definition(rule['definition'], exp_param_specs_dict)

            if rule['dest'] not in state_name_list + [feconf.END_DEST]:
                raise Exception('Destination %s is invalid.' % rule['dest'])

            # Check that there are no feedback-less self-loops. 
            # NB: Sometimes it makes sense for a self-loop to not have
            # feedback, such as unreachable rules in a ruleset for multiple-
            # choice questions. This should be handled in the frontend so
            # that a valid dict with feedback for every self-loop is always
            # saved to the backend.
            if (rule['dest'] == curr_state_name and not rule['feedback']
                    and not state_dict['widget']['sticky']):
                raise Exception('State "%s" has a self-loop with no feedback. '
                                'This is likely to frustrate the reader.' %
                                curr_state_name)

            _verify_param_changes(rule['param_changes'], exp_param_specs_dict)

    for wp_name, wp_value in (
            state_dict['widget']['customization_args'].iteritems()):
        if not isinstance(wp_name, basestring):
            raise Exception('Invalid widget customization arg name: %s'
                            % wp_name)

        try:
            widget = widget_registry.Registry.get_widget_by_id(
                feconf.INTERACTIVE_PREFIX, state_dict['widget']['widget_id'])
        except Exception as e:
            raise Exception(
                '%s; widget id: %s' % (e, state_dict['widget']['widget_id']))

        widget_param_names = [wp.name for wp in widget.params]
        if wp.name not in widget_param_names:
            raise Exception('Parameter %s for widget %s is invalid.' % (
                wp_name, state_dict['widget']['widget_id']))

        # Get the object class used to normalize the value for this param.
        for wp in widget.params:
            if wp.name == wp_name:
                # Ensure that the object type exists.
                obj_services.Registry.get_object_class_by_type(wp.obj_type)
                break

        # TODO(sll): Find a way to verify that the widget parameter values
        # have the correct type. Can we get sample values for the context
        # parameters?

def _verify_all_states_reachable(states_list):
    """Verifies that all states are reachable from the initial state."""

    # This queue stores state names.
    processed_queue = []
    curr_queue = [states_list[0]['name']]

    while curr_queue:
        curr_state = curr_queue[0]
        curr_queue = curr_queue[1:]

        if curr_state in processed_queue:
            continue

        processed_queue.append(curr_state)

        curr_state_ind = next(ind for ind, state in enumerate(states_list)
                              if state['name'] == curr_state)

        for handler in states_list[curr_state_ind]['widget']['handlers']:
            for rule in handler['rule_specs']:
                dest_state = rule['dest']
                if (dest_state not in curr_queue and
                        dest_state not in processed_queue and
                        dest_state != feconf.END_DEST):
                    curr_queue.append(dest_state)

    if len(states_list) != len(processed_queue):
        unseen_states = list(
            set([s['name'] for s in states_list]) - set(processed_queue))
        raise Exception('The following states are not reachable from the '
                        'initial state: %s' % ', '.join(unseen_states))

def _verify_no_dead_ends(states_list):
    """Verifies that the END state is reachable from all states."""

    # This queue stores state names.
    processed_queue = []
    curr_queue = [feconf.END_DEST]

    while curr_queue:
        curr_state = curr_queue[0]
        curr_queue = curr_queue[1:]

        if curr_state in processed_queue:
            continue

        if curr_state != feconf.END_DEST:
            processed_queue.append(curr_state)

        for ind, state in enumerate(states_list):
            state_name = state['name']
            if (state_name not in curr_queue
                    and state_name not in processed_queue):
                state_widget = states_list[ind]['widget']
                for handler in state_widget['handlers']:
                    for rule in handler['rule_specs']:
                        if rule['dest'] == curr_state:
                            curr_queue.append(state_name)
                            break

    if len(states_list) != len(processed_queue):
        dead_end_states = list(
            set([s['name'] for s in states_list]) - set(processed_queue))
        raise Exception('The END state is not reachable from the '
                        'following states: %s' %
                        ', '.join(dead_end_states))

def verify_exploration_dict(exploration_dict):
    """Verifies an exploration dict."""
    EXPLORATION_SCHEMA = [
        ('default_skin', basestring), ('param_changes', list),
        ('param_specs', dict), ('schema_version', int), ('states', list)
    ]
    utils.verify_dict_keys_and_types(exploration_dict, EXPLORATION_SCHEMA)

    # Each param spec value should be a dict of the form {obj_type: [STRING]}.
    for param_key in exploration_dict['param_specs']:
        ps_value = exploration_dict['param_specs'][param_key]
        if len(ps_value) != 1 or ps_value.keys()[0] != 'obj_type':
            raise Exception('Invalid param_spec dict: %s' % ps_value)

        # Ensure that the object type exists.
        obj_services.Registry.get_object_class_by_type(ps_value['obj_type'])

    # Verify there is at least one state.
    if not exploration_dict['states']:
        raise Exception('Each exploration should have at least one state.')

    state_name_list = []
    for state_desc in exploration_dict['states']:
        state_name = state_desc['name']
        if state_name in state_name_list:
            raise Exception('Duplicate state name: %s' % state_name)
        state_name_list.append(state_name)

    for state_desc in exploration_dict['states']:
        verify_state_dict(
            state_desc, state_name_list, exploration_dict['param_specs'])

    _verify_all_states_reachable(exploration_dict['states'])
    _verify_no_dead_ends(exploration_dict['states'])
