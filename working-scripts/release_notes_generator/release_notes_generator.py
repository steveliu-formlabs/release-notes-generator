#!/usr/bin/python3
import re
import os
import subprocess
import requests
import time
import tempfile


# github
GITHUB_COMMIT_URL = 'https://github.com/Formlabs/factory-software/commit/'
GITHUB_PULL_URL = 'https://github.com/Formlabs/factory-software/pull/'

# jira
#
# 1) Atlassian API TOKEN (https://confluence.atlassian.com/cloud/api-tokens-938839638.html)
#
#   i.e: curl -v https://mysite.atlassian.net --user me@example.com:my-api-token
#
# 2) JIRA Server Get Issue API (https://docs.atlassian.com/software/jira/docs/api/REST/7.6.1/#api/2/issue-getIssue)
#
#   i.e: curl \
#           -u steve.liu@formlabs.com:WEG7yCBj03YHnwPJkDRL34F7 \
#           -X GET \
#           https://formlabs.atlassian.net/rest/api/2/issue/FT-1778
#
JIRA_TICKET_URL = 'https://formlabs.atlassian.net/browse/'
JIRA_ISSUE_REST_API = 'https://formlabs.atlassian.net/rest/api/2/issue/'
JIRA_ACCOUNT = 'steve.liu@formlabs.com'
JIRA_TOKEN = 'WEG7yCBj03YHnwPJkDRL34F7'

# regex
FT_REGEX = '[A-Z]{2}-[0-9]+|$'
GITHUB_PULL_ID_REGEX = ' #[0-9]+ |$'
RELEASE_BRANCH_REGEX = 'release/[0-1a-z-]+/(?:[0-9]+\.)+[0-9]+'
VERSION_REGEX = '(?:[0-9]+\.)+[0-9]+|$'

# misc
COMPONENT_FILE_PATH = 'components/{}/release-notes.md'


def get_current_branch():
    """Get current branch name.
    """
    # find all tags
    cmd = ['git', 'branch']
    out = subprocess.check_output(cmd).decode('unicode_escape')
    lines = out.strip().split('\n')
    for line in lines:
        if line.startswith('* '):
            return line[2:].strip()


def fetch_github_release():
    """Return a dictionary contain the branches and version numbers.
    """
    # tags sort by version number
    cmd = ['git', 'tag']
    out = subprocess.check_output(cmd)
    cmd = ['sort', '-V']
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    out = proc.communicate(out)[0].decode('utf-8')
    tag_names = re.findall(RELEASE_BRANCH_REGEX, out)
    if len(tag_names) == 0:
        raise ValueError('No Tags matching pattern {} found.'.format(RELEASE_BRANCH_REGEX))

    # find the first commit
    cmd = ['git', 'log', '--pretty=format:%H', '--reverse']
    out = subprocess.check_output(cmd).decode('unicode_escape')
    first_commit_id = out.strip().split()[0]

    # group the tags under component
    component_tags = {}
    for tag_name in tag_names:
        _, component, version = tag_name.split('/')
        cmd = ['git', 'rev-list', '-n', '1', tag_name]
        out = subprocess.check_output(cmd).decode('unicode_escape')
        if component not in component_tags:
            # the root commit
            component_tags[component] = [{'tag_name': '', 'tag_commit_id': first_commit_id}]
        component_tags[component].append({'tag_name': tag_name, 'tag_commit_id': out.strip()})

    # find ancestor:
    #
    #   --------*-------------------*------------------------------------------- master
    #                                                   \                \
    #           ^                   ^                    \                \
    #           ^                   ^                     \                \
    #   sfwc-liveusb/1.1.0    sfwc-liveusb/1.1.1           ---*             -----------*
    #
    #                                                         ^                        ^
    #                                                         ^                        ^
    #                                                   sfwc-liveusb/1.1.3        sfwc-liveusb/1.1.4
    #
    #
    # Conclusion:
    #
    #   The ancestor of `sfwc-liveusb/1.1.3` should be `sfwc-liveusb/1.1.1`.
    #   The ancestor of `sfwc-liveusb/1.1.4` should also be `sfwc-liveusb/1.1.1`.
    #
    for component, tags in component_tags.items():
        commit_id_tag_name = {}
        for tag in tags:
            commit_id_tag_name[tag['tag_commit_id']] = tag['tag_name']
        for i in range(1, len(tags)):
            for j in range(i - 1, -1, -1):
                # use `git merge-base` to find latest common ancestor
                cmd = ['git', 'merge-base', tags[i]['tag_commit_id'], tags[j]['tag_commit_id']]
                out = subprocess.check_output(cmd).decode('unicode_escape')
                ancestor_commit_id = out.strip()

                # if we have ancestor in our commit cache, it means the tag at index j is our previous release
                if ancestor_commit_id in commit_id_tag_name:
                    tags[i]['pre_tag_name'] = commit_id_tag_name[ancestor_commit_id]
                    tags[i]['pre_tag_commit_id'] = ancestor_commit_id
                    break

    return component_tags


def fetch_github_tickets(tag):
    """Return a list of dict with Github information.
    """
    # find commits between two commits
    cmd = ['git', '--no-pager', 'log', '--format="%ad %H %s"', '--date=short',
           tag['pre_tag_commit_id'] + '...' + tag['tag_commit_id']]
    out = subprocess.check_output(cmd).decode('unicode_escape')

    # find valid tickets mentioned in those commits
    tickets = []
    lines = [line for line in out.split('\n') if line.strip()]
    for line in lines:
        ft = re.findall(FT_REGEX, line)[0]
        pull_id = re.findall(GITHUB_PULL_ID_REGEX, line)[0].strip().strip('#')
        date, commit_id, title = line.strip('"').strip().split(' ', 2)

        # find files changed
        file_changed = False
        dir_path = os.path.join('components', tag['tag_name'].strip('/').split('/')[1])

        cmd = ['git', '--no-pager', 'show', '--stat', commit_id]
        out = subprocess.check_output(cmd).decode('unicode_escape')

        # goes from the end of line to find the matching directory
        files = out.strip('\n').split('\n')
        for f in files[::-1]:
            if f.strip() == '':
                break
            if f.strip().startswith(dir_path):
                file_changed = True
                break

        # a ticket is valid if we found the FT, Pull Request ID and files under the directory changed
        if file_changed:
            tickets.append({'date': date, 'commit_id': commit_id, 'title': title, 'ft': ft, 'pull_id': pull_id})
    return tickets


def fetch_jira_tickets(fts):
    """Return a list of dict which JIRA information.
    """
    tickets = []
    for ft in fts:
        if not ft:
            tickets.append({
                'summary': '',
                'description': '',
                'assignee_name': '',
                'reporter_name': '',
                'priority_name': '',
                'priority_icon_url': '',
                'status_name': '',
                'status_icon_url': '',
                'jira_url': '',
            })
            continue
        url = JIRA_ISSUE_REST_API + ft
        sleep_sec = 2
        for _ in range(3):
            r = requests.get(url, auth=(JIRA_ACCOUNT, JIRA_TOKEN))
            if r.status_code == 200:
                data = r.json()
                tickets.append({
                    'summary': data['fields']['summary'],
                    'description': data['fields']['description'],
                    'assignee_name': data['fields']['assignee']['name'] if data['fields']['assignee'] else '',
                    'reporter_name': data['fields']['reporter']['name'],
                    'priority_name': data['fields']['priority']['name'],
                    'priority_icon_url': data['fields']['priority']['iconUrl'],
                    'status_name': data['fields']['status']['name'],
                    'status_icon_url': data['fields']['status']['iconUrl'],
                    'jira_url': 'https://formlabs.atlassian.net/browse/{}'.format(ft),
                })
                break
            print(r.status_code)
            time.sleep(sleep_sec)
            sleep_sec *= 2
        else:
            raise requests.HTTPError('connection to JIRA with ticket {} failed after 3 times'.format(ft))
    return tickets


def generate_markdown_text(title, headers, rows, tag):
    """Return string for the MarkDown Table.

    A Single Markdown Table:

    ### title

    | header[0]  | header[1]  | header[2]  | header[3]  |
    -----------------------------------------------------
    | rows[0][0] | rows[0][1] | rows[0][2] | rows[0][3] |
    | rows[1][0] | rows[1][1] | rows[1][2] | rows[1][3] |
    | rows[2][0] | rows[2][1] | rows[2][2] | rows[2][3] |
    | rows[3][0] | rows[3][1] | rows[3][2] | rows[3][3] |

    """
    # The length of each row should be equal to the headers
    for row in rows:
        if len(headers) != len(row):
            raise ValueError('In Markdown table, we expect the length of row is equal to the headers.')

    text = '\n'
    # table title
    text += '## {}\n\n'.format(title)
    # table header
    text += '| {} |\n'.format(' | '.join(headers))
    # table separator
    text += '|{}|\n'.format('|'.join(['-' * (len(header) + 2) for header in headers]))
    # tables rows
    for row in rows:
        text += '|' + '|'.join(row) + '|'
        text += '\n'
    text += '\n'
    # pre release tag
    text += '__Previous Release:__ {}\n\n'.format(tag['pre_tag_name'])
    # changes on Github
    text += '__[Compare changes on Github](https://github.com/steveliu-formlabs/release-notes-generator/compare/{}...{})__\n\n'.format(tag['pre_tag_name'], tag['tag_name'])
    # git commands
    text += """\
```
>> git diff {} {}
```
""".format(tag['pre_tag_name'], tag['tag_name'])
    # git commands
    text += '\n'
    return text


def command_prompt_step1(component_tags):
    """Get the component to create or release.
    """
    # prompt the description
    ques = '1. Which component you are going to release?'
    print('{}\n'.format(ques))
    idx = 0
    idx_component = {}
    print('    [0] Release a new component')
    print('    ---------------------------')
    for component, tags in component_tags.items():
        idx += 1
        print('    [{}] {}'.format(idx, component))
        idx_component[idx] = component
    print()

    # get the choose component
    print('Input the number of component [0~{}]: '.format(idx), end='')
    option_number = input()
    try:
        option_number = int(option_number)
    except ValueError:
        print('{} is not an integer'.format(option_number))
    if not 0 <= int(option_number) <= idx:
        raise ValueError('The number of component should be between 0 and {}'.format(idx))

    # further component and version
    if option_number == 0:
        print("Input name of new component: ", end='')
        component = input().strip()

        if not os.path.isdir(os.path.join('components', component)):
            raise ValueError('No {} found under components directory.')

        print('Input the version number: ', end='')
        version = input().strip()

        # check the format of version
        find_version = re.findall(VERSION_REGEX, version)[0]
        if version != find_version:
            raise ValueError('{} is not a valid version format.'.format(find_version))
        print()

        # find the first commit
        cmd = ['git', 'log', '--pretty=format:%H', '--reverse']
        out = subprocess.check_output(cmd).decode('unicode_escape')
        first_commit_id = out.strip().split()[0]

        # get latest commit id
        cmd = ['git', 'log', '--pretty=format:%H']
        out = subprocess.check_output(cmd).decode('unicode_escape')
        latest_commit_id = out.strip().split()[0]

        # root commit & the initial release
        component_tags[component] = [
            {
                'tag_name': '',
                'tag_commit_id': first_commit_id
            },
            {
                'tag_name': 'release/{}/{}'.format(component, version),
                'tag_commit_id': latest_commit_id,
                'pre_tag_name': '',
                'pre_tag_commit_id': first_commit_id,
            }
        ]

    else:
        component = idx_component[option_number]
        cur_version = component_tags[component][-1]['tag_name'].rsplit('/')[-1]
        print('Input the version (current latest version is {}): '.format(cur_version), end='')
        version = input().strip()

        # check the format of version
        find_version = re.findall(VERSION_REGEX, version)[0]
        if version != find_version:
            raise ValueError('{} is not a valid version format.'.format(find_version))
        print()

        # get latest commit id
        cmd = ['git', 'log', '--pretty=format:%H']
        out = subprocess.check_output(cmd).decode('unicode_escape')
        latest_commit_id = out.strip().split()[0]

        # add new tag from user
        if component in component_tags:

            component_tags[component].append({
                'tag_name': 'release/{}/{}'.format(component, version),
                'tag_commit_id': latest_commit_id
            })

            # commit cache
            commit_id_tag_name = {}
            for tag in component_tags[component]:
                commit_id_tag_name[tag['tag_commit_id']] = tag['tag_name']

            # find the ancestor
            for i in range(len(component_tags[component]) - 2, 0, -1):

                # use `git merge-base` to find latest common ancestor
                cmd = ['git', 'merge-base', component_tags[component][i]['tag_commit_id'], latest_commit_id]
                out = subprocess.check_output(cmd).decode('unicode_escape')
                ancestor_commit_id = out.strip()

                # if we have ancestor in our commit cache, it means the tag at index j is our previous release
                if ancestor_commit_id in commit_id_tag_name:
                    component_tags[component][-1]['pre_tag_name'] = commit_id_tag_name[ancestor_commit_id]
                    component_tags[component][-1]['pre_tag_commit_id'] = ancestor_commit_id
                    break

    # return [component, version]
    return component, version


def command_prompt_step2(component_tags):
    """Prompt the Vim to edit the Markdown file.
    """
    # question
    ques = '2. Would you like to add summaries to this release? [Y/N]: '
    print('{}'.format(ques), end='')

    # yes/no flag to open editor
    open_vim = input().strip().lower()
    if open_vim not in ['y', 'n', 'yes', 'no']:
        raise ValueError('Only "Y", "N", "Yes" and "NO" are allowed')

    # TODO: how to integrate into MD?
    if open_vim in ['y', 'yes']:
        editor = os.environ.get('EDITOR', 'vim')

        # Launch editor on an empty temporary file, wait for it to exit, and
        # if it exited successfully, return the contents of the file.
        with tempfile.NamedTemporaryFile() as f:
            f.close()
            try:
                subprocess.check_call([editor, f.name])
            except subprocess.CalledProcessError as e:
                raise IOError('{} exited with code {}.'.format(editor, e.returncode))
            with open(f.name) as g:
                print(g.read())
    else:
        pass

    # check if there is any unstaged or untracked files
    cmd = ['git', 'ls-files', '--other', '--directory', '--exclude-standard']
    out = subprocess.check_output(cmd).decode('unicode_escape')
    if out.strip():
        raise ValueError('You have untracked files. Please stage all the files.')
    cmd = ['git', 'diff-index', '--quiet', 'HEAD', '--']
    try:
        subprocess.check_output(cmd).decode('unicode_escape')
    except subprocess.CalledProcessError:
        raise ValueError('You have uncommited files. Please commit all the changes.')

    # generate notes text
    component_text = {}
    print()
    for component, tags in component_tags.items():
        # Iterate the versions from latest to oldest
        for i in range(len(tags) - 1, 0, -1):
            print('    "{}" release notes is generating...'.format(tags[i]['tag_name']))

            # List all github/jira tickets between them
            github_tickets = fetch_github_tickets(tags[i])
            fts = [ticket['ft'] for ticket in github_tickets]
            jira_tickets = fetch_jira_tickets(fts)

            # Merge two tickets into one
            tickets = [{**github_tickets[i], **jira_tickets[i]} for i in range(len(fts))]

            headers = ['Date', 'Summary', 'Assignee', 'Reporter', 'Priority', 'Status', 'Github', 'JIRA']
            rows = []
            for ticket in tickets:
                rows.append([
                    ticket['date'],
                    ticket['title'],
                    ticket['assignee_name'],
                    ticket['reporter_name'],
                    ticket['priority_name'],
                    ticket['status_name'],
                    '[{}]({}{})'.format(ticket['ft'], GITHUB_PULL_URL, ticket['pull_id']),
                    '[{}]({}{})'.format(ticket['ft'], JIRA_TICKET_URL, ticket['ft']),
                ])

            # concatenate the Markdown text
            if component not in component_text:
                component_text[component] = ''
            text = generate_markdown_text(tags[i]['tag_name'], headers, rows, tags[i])
            component_text[component] += text
        break
    print()

    # write text into the files
    for component, text in component_text.items():
        f_path = COMPONENT_FILE_PATH.format(component)
        print('    "{}" file is generated'.format(f_path))
        with open(f_path, 'w') as f:
            f.write(text)
    print()


def command_prompt_step3_step4(component, version, remote, branch):
    """Stage and commit all the documentations and then tag the commitment.
    """
    tag = 'release/{}/{}'.format(component, version)

    # question
    print('3. The release script is going to run the following COMMIT and TAG commands.')
    print()
    print('    >> git add .')
    print('    >> git commit -m ')
    print('    >> git tag {}'.format(tag))
    print()
    print('Do you want the release script run these commands for you? [Y/N]: ', end='')

    # get the yes/no flag
    run_script = input().strip().lower()
    print()
    if run_script not in ['y', 'n', 'yes', 'no']:
        raise ValueError('Only "Y", "N", "Yes" and "No" are allowed')

    if run_script in ['y', 'yes']:
        # add & commit & tag
        cmd = ['git', 'add', '.']
        subprocess.check_output(cmd).decode('unicode_escape')
        cmd = ['git', 'commit', '-m', 'Release {}'.format(tag)]
        subprocess.check_output(cmd).decode('unicode_escape')
        cmd = ['git', 'tag', '{}'.format(tag)]
        subprocess.check_output(cmd).decode('unicode_escape')

        # list the description
        print('4. The release script is going to run the following PUSH commands.')
        print()
        print('    >> git push {} master'.format(remote, branch))
        print('    >> git push {} {}'.format(remote, tag))
        print()
        print('Do you want the script run these commands for you? [Y/N]: ', end='')

        # get the yes/no flag
        run_script = input().strip().lower()
        if run_script not in ['y', 'n', 'yes', 'no']:
            raise ValueError('Only "Y", "N", "Yes" and "No" are allowed')

        # add & commit & tag
        cmd = ['git', 'push', remote, branch]
        subprocess.check_output(cmd).decode('unicode_escape')
        cmd = ['git', 'push', remote, tag]
        subprocess.check_output(cmd).decode('unicode_escape')

    else:
        # list the description
        print('4. Please type the following commands to commit, tag and push the codes by yourself.')
        print()
        print('    >> git add .')
        print('    >> git commit -m ')
        print('    >> git tag {}'.format(tag))
        print('    >> git push {} {}'.format(remote, branch))
        print('    >> git push {} {}'.format(remote, tag))
        print()


def main():
    # change work directory
    cur_dir = os.path.dirname(os.path.abspath(__file__))
    # use the `os.sep` concatenate ['', 'a', 'b', 'c'] to /a/b/c
    project_dir = os.sep.join(cur_dir.split(os.sep)[:-2])
    os.chdir(project_dir)

    # get branch name
    branch = get_current_branch()
    remote = 'upstream'
    print('0. Current branch is {} and current remote is {}.\n'.format(branch, remote))

    # fetch release tags
    component_tags = fetch_github_release()

    # step 1: choose component
    component, version = command_prompt_step1(component_tags)

    # step 2: open editor and generate docs
    command_prompt_step2(component_tags)

    # step 3 & 4: commit the codes and push the codes
    command_prompt_step3_step4(component, version, remote, branch)


if __name__ == '__main__':
    main()
