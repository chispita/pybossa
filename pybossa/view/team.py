## file is part of PyBOSSA.
#
# PyBOSSA is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# PyBOSSA is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with PyBOSSA.  If not, see <http://www.gnu.org/licenses/>.

from itsdangerous import BadData
from markdown import markdown

from flask import Blueprint
from flask import render_template
from flask import request
from flask import abort
from flask import flash
from flask import redirect
from flask import url_for
from flask.ext.login import login_required, current_user
from flask.ext.mail import Message
from flaskext.wtf import Form, TextField, PasswordField, validators, \
        ValidationError, IntegerField, HiddenInput, SelectField, BooleanField

from pybossa.core import db, mail, signer
import pybossa.validator as pb_validator
import pybossa.model as model
from flask.ext.babel import lazy_gettext, gettext
from sqlalchemy.sql import func, text
from pybossa.model import User, Team, User2Team
from pybossa.util import Pagination
from pybossa.auth import require
from sqlalchemy import or_, func, and_
from pybossa.cache import ONE_DAY, ONE_HOUR
from pybossa.cache import teams as cached_teams
from werkzeug.exceptions import HTTPException
import json

blueprint = Blueprint('team', __name__)

def team_title(team, page_name):
    ''' Show team title generic '''
    if not team:
        return "Team not found"

    if page_name is None:
        return "Team: %s" % (team.name)

    return "Team: %s &middot; %s" % (team.name, page_name)

class TeamForm(Form):
    ''' Modify Team '''
    id = IntegerField(label=None, widget=HiddenInput())
    err_msg = lazy_gettext("Team Name must be between 3 and 35 characters long")

    err_msg_2 = lazy_gettext("The team name is already taken")
    name = TextField(lazy_gettext('Team Name'),
                     [validators.Length(min=3, max=35, message=err_msg),
                     pb_validator.Unique(db.session, Team,
                     Team.name, err_msg_2)])

    err_msg = lazy_gettext(
        "Team Description must be between 3 and 35 characters long")
    description = TextField(lazy_gettext('Description'),
                        [validators.Length(min=3, max=35, message=err_msg)])

    public = BooleanField(lazy_gettext('Public'),default=True)

@blueprint.route('/', defaults={'page': 1})
@blueprint.route('/page/<int:page>')
def index(page):
    ''' Show all teams in a grid'''
    per_page = 24
    count = cached_teams.get_teams_count()
    teams = cached_teams.get_teams_page(page, per_page)
    pagination = Pagination(page, per_page, count)
    if not teams and page != 1:
        abort(404)
    return render_template('team/index.html', teams=teams,
                                              total=count,
                                              title="Teams",
                                              pagination=pagination)

@blueprint.route('/public', defaults={'page': 1})
@blueprint.route('/public/page/<int:page>')
@login_required
def public(page):
    ''' By default show the Public Teams '''
    return teams_show(page, cached_teams.get_public_data, 'public',
                      True, False, gettext('Public Teams')
                     )

@blueprint.route('/<name>/')
def public_profile(name):
    ''' Render the public team profile'''
    team = cached_teams.get_team_summary(name)

    if team:
        return render_template('/team/public_profile.html',
                                title='Public Profile',
                                team=team,
                                manage=True)
    else:
        abort(404)

@blueprint.route('/private/', defaults={'page': 1})
@blueprint.route('/private/page/<int:page>')
@login_required
def private(page):
    if current_user.admin != 1:
        abort(404)

    '''By show the private Teams'''
    return teams_show(page, cached_teams.get_private_teams, 'private',
                      True, False, gettext('Private Teams'))

@blueprint.route('/myteams', defaults={'page': 1})
@blueprint.route('/myteams/page/<int:page>')
@login_required
def myteams(page):
    print 'my teams'
    ''' Render my teams section '''
    if not require.team.create():
        abort(403)

    '''By show the private Teams'''
    return teams_show(page, cached_teams.get_signed_teams, 'myteams',
                      True, False, gettext('My Teams'))

def teams_show(page, lookup, team_type, fallback, use_count, title):
    '''Show team list by type '''
    if not require.team.read():
        abort(403)

    per_page = 5
    teams, count = lookup(page, per_page)
    team_owner = []
    if not current_user.is_anonymous():
        team_owner = Team.query.filter(Team.owner_id==current_user.id).first()

        for team in teams:
            team['belong'] = cached_teams.user_belong_team(team['id'])

    pagination = Pagination(page, per_page, count)
    template_args = {
        "teams": teams,
        "team_owner": team_owner,
        "title": title,
        "pagination": pagination,
        "team_type": team_type
        }

    if use_count:
        template_args.update({"count": count})

    return render_template('/team/teams.html', **template_args)

@blueprint.route('/<name>/settings')
@login_required
def detail(name=None):
    ''' Team details '''
    if not require.team.read():
        abort(403)

    team = cached_teams.get_team(name)
    title = team_title(team, team.name)

    ''' Get extra data '''
    data = dict(
            belong = cached_teams.user_belong_team(team.id),
            members = cached_teams.get_number_members(team.id)
            )
    data['rank'], data['score'] = cached_teams.get_rank(team.id)

    try:
        require.team.read(team)
        template = '/team/settings.html'
    except HTTPException:
        template = '/team/index.html'

    template_args = {
        "team": team,
		"title": title,
		"data": data
        }

    return render_template(template, **template_args)

@blueprint.route('/<type>/search', methods=['GET', 'POST'])
@login_required
def search_teams(type):
    ''' Search Teams '''
    if not require.team.read():
        abort(403)
    
    title = gettext('Search name of teams')
    form = SearchForm(request.form)
    teams = db.session.query(Team).all()

    if request.method == 'POST' and form.user.data:
        query = '%' + form.user.data.lower() + '%'
        if type == 'public':
            founds = db.session.query(Team)\
                       .filter(func.lower(Team.name).like(query))\
                       .filter(Team.public == True)\
                       .all()
        else:
            founds = db.session.query(Team)\
                       .join(User2Team)\
                       .filter(func.lower(Team.name).like(query))\
                       .filter(User2Team.user_id == current_user.id)\
                       .all()
        if not founds:
            msg = gettext('Ooops! We didn\'t find a team matching your query:')
            flash(msg)

            return render_template(
                '/team/search_teams.html',
                founds= [],
                team_type = type,
                title=gettext('Search Team'))
        else:
            return render_template(
                '/team/search_teams.html',
                founds = founds,
                team_type = type,
                title = gettext('Search Team'))

    return render_template(
            '/team/search_teams.html',
            found = [],
            team_type = type,
            title = gettext('Search Team'))

@blueprint.route('/<name>/users/search', methods=['GET', 'POST'])
@login_required
def search_users(name):
    ''' Search users in a team'''
    if not require.team.read():
        abort(403)

    team = cached_teams.get_team(name)
    form = SearchForm(request.form)
    users = db.session.query(User).all()

    if request.method == 'POST' and form.user.data:
        query = '%' + form.user.data.lower() + '%'
        founds = db.session.query(User)\
                  .filter(or_(func.lower(User.name).like(query),
                              func.lower(User.fullname).like(query)))\
                  .all()

        if not founds:
            msg = gettext('Ooops!  We didn\'t find a user matching your query:')
            flash(msg)

            return render_template(
                '/team/search_users.html',
                founds = [],
                team = team,
                title = gettext('Search name of User'))
        else:
            for found in founds:
                user2team = User2Team.query\
                                .filter(User2Team.team_id==team.id)\
                                .filter(User2Team.user_id==found.id)\
                                .first()
                found.belong = (1, 0)[user2team is None]

            return render_template(
                '/team/search_users.html',
                founds = founds,
                team = team,
                title = gettext('Search User'))

    return render_template(
        '/team/search_users.html',
        founds = [],
        team = team,
        title = gettext('Search User'))

class SearchForm(Form):
    ''' Search User Form Generic '''
    user = TextField(lazy_gettext('User'))

@blueprint.route('/new', methods=['GET', 'POST'])
@login_required
def new():
    ''' Creation of new team '''
    if not require.team.create():
        abort(403)

    form = TeamForm(request.form)

    def respond(errors):
        return render_template(
            'team/new.html',
            title = gettext('Create a Team'),
            form=form, errors=errors)

    if request.method != 'POST':
        return respond(False)

    if not form.validate():
        flash(gettext('Please correct the errors'), 'error')
        return respond(True)

    team = Team(
        name=form.name.data,
        description=form.description.data,
        public=form.public.data,
        owner_id=current_user.id
        )

    ''' Insert into the current user in the new group '''
    try:
        cached_teams.delete_team_summary()
        print "delete_team_summary"
        db.session.add(team)
        db.session.commit()

        user2team = User2Team( user_id = current_user.id,
                          team_id = team.id)

        db.session.add(user2team)
        db.session.commit()
        flash(gettext('Team created'), 'success')
        return redirect(url_for('.detail', name=team.name))

    except Exception as e:
        flash( e ,'error')
        return redirect(url_for('.myteams'))

@blueprint.route('/<name>/users')
def users(name):
    ''' Add new user to a team '''
    team = cached_teams.get_team(name)

    title = gettext('Team Members')

    if not require.team.read():
        abort(403)

    users = cached_teams.get_users_teams_detail(team.id)

    # Search users in the team
    belongs = User2Team.query.filter(User2Team.team_id == team.id)\
                            .all()

    template = '/team/users.html'
    template_args = {
        "team": team,
        "users": users,
        "belongs": belongs,
        "title": title
        }

    return render_template(template, **template_args)

@blueprint.route('/<name>/delete', methods=['GET', 'POST'])
@login_required
def delete(name):
    ''' Delete the team owner of de current_user '''
    team = cached_teams.get_team(name)
    title = gettext('Delete Team')

    if not require.team.delete(team):
        abort(403)

    if request.method == 'GET':
        return render_template(
            '/team/delete.html',
            title=title,
            team=team)

    print "delete_team_summary"
    cached_teams.delete_team_summary()
    db.session.delete(team)
    db.session.commit()

    flash(gettext('Team deleted!'), 'success')
    return redirect(url_for('team.myteams'))

@blueprint.route('/<name>/update', methods=['GET', 'POST'])
@login_required
def update(name):
    ''' Update the team owner of the current user '''
    team = cached_teams.get_team(name)

    def handle_valid_form(form):
        new_team = Team(
            id=form.id.data,
            name=form.name.data,
            description=form.description.data,
            public=form.public.data
            )
        cached_teams.delete_team_summary()
        db.session.merge(new_team)
        db.session.commit()
        flash(gettext('Team updated!'), 'success')
        return redirect(url_for('.detail',name=new_team.name))

    if not require.team.update(team):
        abort(403)

    title = gettext('Update Team')
    if request.method == 'GET':
        form = TeamForm(obj=team)
        form.populate_obj(team)

    if request.method == 'POST':
        form = TeamForm(request.form)
        if form.validate():
            return handle_valid_form(form)
        flash(gettext('Please correct the errors'), 'error')

    return render_template(
        '/team/update.html',
        form=form,
        title=title,
        team=team)

@blueprint.route('/<name>/join', methods=['GET', 'POST'])
@blueprint.route('/<name>/join/<user>', methods=['GET', 'POST'])
@login_required
def user_add(name,user=None):
    ''' Add Current User to a team '''
    team = cached_teams.get_team(name)
    title = gettext('Add User to a Team')

    if not require.team.read():
        abort(403)

    if request.method == 'GET':
        return render_template(
            '/team/user_add.html',
            title=title,
            team=team,
            user=user
            )

    if user:
        user_search = User.query.filter_by(name=user).first()
        if not user_search:
            flash(gettext('This user don\t exists!!!'), 'error')
            return redirect(url_for('team.myteams',  name=team.name ))
        else:
            ''' Check to see if the current_user is the owner or admin '''
            if current_user.admin is True or team.owner_id == current_user.id:
                user_id = user_search.id
            else:
                flash(gettext('You do not have right to add to this team!!!'), 'error')
                return redirect(url_for('team.myteams',  name=team.name ))
    else:
	user_search= current_user
        '''user_id = current_user.id'''

    ''' Search relationship '''
    user2team = db.session.query(User2Team)\
                .filter(User2Team.user_id == user_search.id )\
                .filter(User2Team.team_id == team.id )\
                .first()

    if user2team:
        flash(gettext('This user is already in this team'), 'error')
        return redirect(url_for('team.search_users',  name=team.name ))

    else:
        if team.public == True:
            cached_teams.delete_team_members()
            user2team = User2Team(
                        user_id = user_search.id,
                        team_id = team.id
                        )
            db.session.add(user2team)
            db.session.commit()
            flash(gettext('Association to the team created'), 'success')
            return redirect(url_for('team.myteams' ))

        else:
            msg = Message(subject='Invitation to a Team',
                            recipients=[user_search.email_addr])

            userdict = {'user': user_search.name, 
                        'team': team.name
                        }

            key = signer.dumps(userdict, salt='join-private-team')

            join_url = url_for('.join_private_team',
                                key=key, _external=True)
            msg.body = render_template(
                '/team/email/send_invitation.md',
                user=user_search, team=team, join_url=join_url)
            msg.html = markdown(msg.body)
            mail.send(msg)

            return render_template('./team/message.html')

@blueprint.route('/join-private-team', methods=['GET', 'POST'])
@login_required
def join_private_team():
    key = request.args.get('key')
    if key is None:
        abort(403)
    userdict = {}
    try:
        userdict = signer.loads(key, max_age=3600, salt='join-private-team')
    except BadData:
        abort(403)

    username = userdict.get('user')
    teamname = userdict.get('team')
    if not username or not teamname or current_user.name != username:
        abort (403)

    ''' Add to Public with invitation team '''
    team = cached_teams.get_team(teamname)
    if not team:
        flash(gettext('This team doesn\'t exists'), 'error')
        return redirect(url_for('team.myteams'))

    ''' Search relationship '''
    user2team = db.session.query(User2Team)\
                .filter(User2Team.user_id == current_user.id)\
                .filter(User2Team.team_id == team.id )\
                .first()

    if user2team:
        flash(gettext('This user is already in this team'), 'error')
        return redirect(url_for('team.users',  name=team.name ))
    else:
        user2team = User2Team(user_id = current_user.id,
                              team_id = team.id
                              )
        cached_teams.delete_team_summary()
        db.session.add(user2team)
        db.session.commit()
        flash(gettext('Congratulations! You belong to the Public Invitation Only Team'), 'sucess')
        return redirect(url_for('team.users',  name=team.name ))

@blueprint.route('/<name>/separate', methods=['GET', 'POST'])
@blueprint.route('/<name>/separate/<user>', methods=['GET', 'POST'])
@login_required
def user_delete(name,user=None):
    team = cached_teams.get_team(name)
    title = gettext('Delete User from a Team')
        
    if not require.team.read():
        abort(403)
                           
    if request.method == 'GET':
        return render_template('/team/user_separate.html',
                               title=title,
                               team=team,
                               user=user
                                )
    if user:
        user_search = User.query.filter_by(name=user).first()
        if not user_search:
            flash(gettext('This user don\t exists!!!'), 'error')
            return redirect(url_for('team.myteams',  name=team.name ))
        else:
            ''' Check to see if the current_user is the owner or admin '''
            if current_user.admin is True or team.owner_id == current_user.id:
                user_id = user_search.id
            else:
                flash(gettext('You do not have right to separate to this team!!!'), 'error')
                return redirect(url_for('team.myteams',  name=team.name ))
    else:
        user_id = current_user.id

    ''' Check if exits association'''
    user2team = db.session.query(User2Team)\
                                    .filter(User2Team.user_id == user_id )\
                                    .filter(User2Team.team_id == team.id )\
                                    .first()

    if user2team:
        cached_teams.delete_team_members()
        db.session.delete(user2team)
        db.session.commit()
        flash(gettext('Association to the team deleted'), 'success')

    return redirect(url_for('team.myteams'))
