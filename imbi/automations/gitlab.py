import asyncio
import dataclasses
import pathlib
import re
import tempfile
import typing

import cookiecutter.main
import flatdict
import isort
import sprockets_postgres
from yapf import yapf_api

from imbi import models, oauth2
from imbi.automations import base
from imbi.clients import gitlab, sonarqube
if typing.TYPE_CHECKING:
    from imbi import app, user


class GitLabCreateProjectAutomation(base.Automation):

    INSERT_SQL = re.sub(r'\s+', ' ', """\
        INSERT INTO v1.project_links
                    (project_id, link_type_id, created_by, url)
             VALUES (%(project_id)s, %(link_type_id)s,
                     %(username)s, %(url)s)""")

    UPDATE_SQL = re.sub(r'\s+', ' ', """\
        UPDATE v1.projects
           SET gitlab_project_id = %(gitlab_project_id)s
         WHERE id = %(project_id)s""")

    def __init__(self,
                 application: 'app.Application',
                 project_id: int,
                 current_user: 'user.User',
                 db: sprockets_postgres.PostgresConnector):
        super().__init__(application, current_user, db)
        self.imbi_project_id = project_id
        self._gitlab: typing.Optional[gitlab.GitLabClient] = None
        self._gitlab_parent: typing.Optional[dict] = None
        self._project: typing.Optional[models.Project] = None

    async def prepare(self) -> typing.List[str]:
        project: models.Project
        token: oauth2.IntegrationToken
        project, token = await asyncio.gather(
            self._get_project(self.imbi_project_id),
            self._get_gitlab_token())
        if project is not None and project.gitlab_project_id is not None:
            self._add_error(
                'GitLab project {} already exists for {}',
                project.gitlab_project_id, project.slug)
        elif project is not None and token is not None:
            self._gitlab = gitlab.GitLabClient(token, self.application)
            self._gitlab_parent = await self._get_gitlab_parent(project)
            self._project = project
        return self.errors

    async def run(self) -> gitlab.ProjectInfo:
        project = await self._gitlab.create_project(
            self._gitlab_parent,
            self._project.name,
            description=self._project.description)
        await self.db.execute(
            self.UPDATE_SQL,
            {
                'project_id': self._project.id,
                'gitlab_project_id': project.id,
            })

        link_id = self.automation_settings['gitlab'].get(
            'project_link_type_id')
        if link_id:
            await self.db.execute(
                self.INSERT_SQL,
                {
                    'link_type_id': link_id,
                    'project_id': self._project.id,
                    'url': project.web_url,
                    'username': self.user.username,
                })
        return project

    async def _get_gitlab_parent(self, project:  models.Project) -> dict:
        gitlab_parent = await self._gitlab.fetch_group(
            project.namespace.gitlab_group_name,
            project.project_type.gitlab_project_prefix)
        if not gitlab_parent:
            self._add_error(
                'GitLab path {}/{} does not exist',
                project.namespace.gitlab_group_name,
                project.project_type.gitlab_project_prefix)
        return gitlab_parent


class GitLabInitialCommitAutomation(base.Automation):

    GET_COOKIE_CUTTER = re.sub(r'\s+', ' ', """\
        SELECT name, project_type_id, url
          FROM v1.cookie_cutters
         WHERE type='project'
           AND url = %(url)s""")

    def __init__(self,
                 application: 'app.Application',
                 project_id: int,
                 cookie_cutter: str,
                 current_user: 'user.User',
                 db: sprockets_postgres.PostgresConnector):
        super().__init__(application, current_user, db)
        self._cookie_cutter_name = cookie_cutter
        self._imbi_project_id = project_id
        self._cookie_cutter: typing.Optional[models.CookieCutter] = None
        self._gitlab: typing.Optional[gitlab.GitLabClient] = None
        self._gitlab_project: typing.Optional[gitlab.ProjectInfo] = None
        self._project: typing.Optional[models.Project] = None
        self._token: typing.Optional[oauth2.IntegrationToken] = None

    async def prepare(self) -> typing.List[str]:
        self._project = await self._get_project(self._imbi_project_id)
        self._token = await self._get_gitlab_token()
        self._cookie_cutter = await self._get_cookie_cutter(
            self._cookie_cutter_name)

        if not self._has_error() and (self._project.project_type.id
                                      != self._cookie_cutter.project_type_id):
            self._add_error(
                'Cookie Cutter {} is not available for Project Type {}',
                self._cookie_cutter.name, self._project.project_type.slug)
        elif self._project.gitlab_project_id is None:
            self._add_error('GitLab project does not exist for {}',
                            self._project.slug)
        else:
            self._gitlab = gitlab.GitLabClient(self._token, self.application)
            self._gitlab_project = await self._gitlab.fetch_project(
                self._project.gitlab_project_id)
            if self._gitlab_project is None:
                self._add_error('GitLab project id {} does not exist',
                                self._project.gitlab_project_id)
        return self.errors

    async def run(self):
        self.logger.info('generating initial commit for %s (%s) from %s',
                         self._project.slug, self._project.id,
                         self._cookie_cutter.url)
        package_name = self._project.slug.lower().replace('-', '_')
        context = dataclasses.asdict(self._project)
        links = {k.lower().replace(' ', '_'): v
                 for k, v in context['links'].items()}
        urls = {k.lower().replace(' ', '_'): v
                for k, v in context['urls'].items()}
        context.update({
            'environments': ','.join(self._project.environments),
            'gitlab_namespace_id': self._gitlab_project.namespace.id,
            'gitlab_url': self._gitlab_project.web_url,
            'links': links,
            'package_name': package_name,
            'sentry_team': None,
            'legacy_sentry_dsn': None,
            'sentry_dsn': None,
            'sentry_slug': None,
            'sentry_dashboard': None,
            'sentry_organization': None,
            'urls': urls
        })
        if self.automation_settings['sonarqube'].get('url'):
            context.update({
                'sonar_project_key': sonarqube.generate_key(self._project),
                'sonar_project_url': sonarqube.generate_dashboard_link(
                    self._project, self.automation_settings['sonarqube'])
            })

        context = flatdict.FlatDict(context, '_').as_dict()
        self.logger.debug('Context %r', context)
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.logger.debug('expanding %s for project %s in %s',
                              self._cookie_cutter.url, self._project.id,
                              tmp_dir)
            project_dir = cookiecutter.main.cookiecutter(
                self._cookie_cutter.url,
                extra_context=context,
                no_input=True,
                output_dir=tmp_dir)
            project_dir = pathlib.Path(project_dir)

            self.logger.debug('reformatting project files')
            isort_cfg = self.automation_settings['isort']
            isort_cfg.setdefault('known_first_party', [])
            isort_cfg['known_first_party'].append(context['package_name'])
            yapf_style = self.automation_settings['yapf']

            for py_file in project_dir.rglob('*.py'):
                isort.api.sort_file(py_file, **isort_cfg)
                yapf_api.FormatFile(
                    str(py_file), style_config=yapf_style,
                    in_place=True, logger=None)

            self.logger.debug('committing to GitLab')
            commit_info = await self._gitlab.commit_tree(
                self._gitlab_project, project_dir,
                'Initial commit (automated)')

            return commit_info

    async def _get_cookie_cutter(self, url: str) \
            -> typing.Optional[models.CookieCutter]:
        result = await self.db.execute(
            self.GET_COOKIE_CUTTER, {'url': url})
        if result.row_count != 0:
            return models.CookieCutter(**result.row)
        else:
            self._add_error('Cookie cutter {} does not exist', url)
