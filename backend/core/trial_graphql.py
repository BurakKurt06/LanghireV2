"""Reviewed Ashby public client operation fingerprints (2026-10-20).

Full hashes retain the reviewed documents; dispatch hashes authorize only their
exact operation/variables/root invocations while allowing read-selection growth.
An operationName alone never grants a mutation. Source/evidence: TESTING.md.
"""
import hashlib
import json
import re

def _tokens(source):
    if not isinstance(source, str) or len(source) > 200_000:
        return None
    tokens, position = [], 0
    pattern = re.compile(r'[ \t\r\n]+|#[^\r\n]*|,|\.\.\.|[_A-Za-z][_0-9A-Za-z]*|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|"(?:[^"\\\x00-\x1f]|\\["\\/bfnrt]|\\u[0-9a-fA-F]{4})*"|[!$():=@\[\]{}|&]')
    while position < len(source):
        if source.startswith('"""', position):
            return None  # block strings are outside the reviewed client subset
        match = pattern.match(source, position)
        if not match:
            return None
        token = match[0]
        position = match.end()
        if token.isspace() or token.startswith("#") or token == ",":
            continue
        tokens.append(["string", json.loads(token)] if token.startswith('"') else token)
    return tokens


def _digest(tokens):
    return hashlib.sha256(json.dumps(tokens, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def query_digest(source):
    tokens = _tokens(source)
    return _digest(tokens) if tokens is not None else ""


def dispatch_digest(source):
    """Pin the operation, variable declarations and every root invocation.

    Read selections may grow when the vendor updates its UI. They cannot add
    another operation/root mutation or change any root alias/arguments. Root
    fragment spreads are refused rather than trusting an operationName.
    This strict subset accepts the vendor's generated documents, not arbitrary
    GraphQL dialects or batches.
    """
    tokens = _tokens(source)
    if not tokens or tokens[0] not in ("query", "mutation"):
        return ""
    def group(index, opening, closing):
        if tokens[index] != opening:
            raise ValueError("Expected group")
        depth = 1
        while depth:
            index += 1
            if index >= len(tokens):
                raise ValueError("Unclosed group")
            if tokens[index] == opening:
                depth += 1
            elif tokens[index] == closing:
                depth -= 1
        return index + 1
    try:
        # Named operation, optional variable declarations, then root selection.
        if not isinstance(tokens[1], str) or not re.fullmatch(r"[_A-Za-z][_0-9A-Za-z]*",tokens[1]):
            return ""
        index = 2
        if tokens[index] == "(":
            index = group(index, "(", ")")
        if tokens[index] != "{":
            return ""
        prefix, roots = tokens[:index], []
        index += 1
        while tokens[index] != "}":
            if tokens[index] == "...":
                return ""  # never hide a dispatch in a root fragment
            start = index
            if not isinstance(tokens[index], str) or not re.fullmatch(r"[_A-Za-z][_0-9A-Za-z]*",tokens[index]):
                return ""
            index += 1
            if tokens[index] == ":":  # pin the actual field as well as alias
                index += 2
            if tokens[index] == "(":
                index = group(index, "(", ")")
            if tokens[index] != "{":
                return ""  # reviewed roots return objects; no new directives
            roots.append(tokens[start:index])
            index = group(index, "{", "}")
        index += 1
        # Only ordinary nested fragment definitions may follow, never a
        # second query/mutation/anonymous operation or fragment on the root.
        while index < len(tokens):
            if (tokens[index] != "fragment" or tokens[index+2] != "on"
                    or tokens[index+3] in ("Query", "Mutation", "Subscription")):
                return ""
            index = group(index+4, "{", "}")
        return _digest([prefix, roots])
    except (IndexError, ValueError, TypeError):
        return ""

OPERATIONS = {'ApiJobBoardWithTeams': {'digest': 'b54778b587834e56709409b5e7ac7550192d36911edaa9f13154c2e0010b5027',
                          'variables': ['organizationHostedJobsPageName'],
                          'roots': ['jobBoardWithTeams'],
                          'dispatch_digest': 'f604ee11743cf3375ff7fecd22b122df457b0a2da11cf304580cd65aeb389604'},
 'ApiJobPosting': {'digest': 'c5a57c2f00d5ba450ac8d595cf677847bc754ff8323e2820778246fd46feb3cf',
                   'variables': ['organizationHostedJobsPageName', 'jobPostingId'],
                   'roots': ['jobPosting'],
                   'dispatch_digest': '07bb53845c99b27e42994d0e3d858779a45302f232acb49cbbecf40d5f611429'},
 'ApiJobPostingAppData': {'digest': 'bce0d31b23c957e8f7c61c32f989d262639eb535b0fbd03b1ed14e00063a1a27',
                          'variables': ['organizationHostedJobsPageName', 'jobPostingId'],
                          'roots': ['jobPosting'],
                          'dispatch_digest': '3e511e343cc82e49ae3ce9d42913ebd9f7f9c9724f59115c8b0752bb43924364'},
 'ApiJobPostingForApplicationRequest': {'digest': 'ed78d7e305359fa0368f6849b42bc26fd45b49e5fe3a6fd2aad4e7fe154a3e63',
                                        'variables': ['organizationHostedJobsPageName',
                                                      'applicationRequestId'],
                                        'roots': ['jobPostingForApplicationRequest'],
                                        'dispatch_digest': '13bc7bf8e138a265b43dffb5e089ddfefd85d610cd9df3589815ea63c0f7f416'},
 'ApiSetFormValue': {'digest': '22cdf739fb47a43ed0cde170cd036d0093d655dbc1efab019be1aa6224b083f9',
                     'variables': ['organizationHostedJobsPageName',
                                   'formRenderIdentifier',
                                   'path',
                                   'value',
                                   'formDefinitionIdentifier'],
                     'roots': ['setFormValue'],
                     'dispatch_digest': '6ec3bb789894335e6c6c195c90b252294e1f95081f13c0d1f12b12c084ec76bf'},
 'ApiSetFormValueToFile': {'digest': '9418249094a43f5626092cacd651560325bf30d2c03ccbf160f22861407f361a',
                           'variables': ['organizationHostedJobsPageName',
                                         'formRenderIdentifier',
                                         'path',
                                         'fileHandle',
                                         'formDefinitionIdentifier'],
                           'roots': ['setFormValueToFile'],
                           'dispatch_digest': '01d817697c76385aef64a4455477223778306564ae14f089c8c4a9b32809fcb4'},
 'ApiAddManyFilesToFormValue': {'digest': '33fcb7c6825f690154a3d12386d5c12ca242e4015059cbba46311b63343e1e50',
                                'variables': ['organizationHostedJobsPageName',
                                              'formRenderIdentifier',
                                              'path',
                                              'fileHandles',
                                              'formDefinitionIdentifier'],
                                'roots': ['addManyFilesToFormValue'],
                                'dispatch_digest': '63fffbc0ddc49eea1911be35badc5e20ddd48b3a2e1a959aede4c760921b69c8'},
 'ApiCreateFileUploadHandle': {'digest': '34a25922eda76bf4cdd5d6d7b138d8f1d779d8349f6e48af17cf9f03cbebfd95',
                               'variables': ['organizationHostedJobsPageName',
                                             'fileUploadContext',
                                             'filename',
                                             'contentType',
                                             'contentLength'],
                               'roots': ['createFileUploadHandle'],
                               'dispatch_digest': '66ead408bd818775e2b9752fd48a7908a585e154bdc49f1a7e8c6d9db6fed660'},
 'ApiRemoveFileFromFormValue': {'digest': '4232429499899ba78cf8fa7c501165950fda3f3b01cbed31bbb6143d396df112',
                                'variables': ['organizationHostedJobsPageName',
                                              'formRenderIdentifier',
                                              'path',
                                              'fileId'],
                                'roots': ['removeFileFromFormValue'],
                                'dispatch_digest': '5e31915047c805a2aa008dd9cc3be57dd15d90e9d7678d7afb472a657f0f0e35'},
 'ApiAutofillApplicationFormWithUploadedResume': {'digest': 'c19bc0d7cd7df24f6c8010993d94b6fc4c3bd1d7cbc860fa4ffb452f2ea11597',
                                                  'variables': ['organizationHostedJobsPageName',
                                                                'formRenderIdentifier',
                                                                'fileHandle',
                                                                'recaptchaToken',
                                                                'formDefinitionIdentifier'],
                                                  'roots': ['autofillApplicationFormWithUploadedResumeHandle'],
                                                  'dispatch_digest': '41d99cb98fef5af4eafe7d33673ddf4e61fda7f3d5749937fa70df310746a5e1'},
 'ApiSubmitCandidateTextingConsent': {'digest': '4b2b3b89f989353fc2037c41bf33b1b3342484b7b21d3a3e247d7a5acdbb94f8',
                                      'variables': ['organizationHostedJobsPageName',
                                                    'formRenderIdentifier',
                                                    'formDefinitionIdentifier',
                                                    'consent'],
                                      'roots': ['submitCandidateTextingConsent'],
                                      'dispatch_digest': '65b668b81fcf7a58ed5fb86da0ee114e9641307777a647bbdf77fa5ba77c70ab'},
 'ApiSubmitCandidateWhatsAppConsent': {'digest': '1e5035395fe05e1b55fa36f3d6b28c592ed16d3c9cefb33d69d8b2ed8e6adae7',
                                       'variables': ['organizationHostedJobsPageName',
                                                     'formRenderIdentifier',
                                                     'formDefinitionIdentifier',
                                                     'consent'],
                                       'roots': ['submitCandidateWhatsAppConsent'],
                                       'dispatch_digest': '9c41ca893997a84154bf3f237d8db596adaa8d7608ff6eed49d057a212878c68'},
 'ApiAutocompleteGeoLocation': {'digest': '3c54636d6c8093cb16b305afb3ca5ff0e42f1590ae7c25e74f8920eaca9d1bc9',
                                'variables': ['text', 'locationTypes'],
                                'roots': ['autocompleteGeoLocation'],
                                'dispatch_digest': '9dde3228e8370a32c138f4febf311e0a7265d0df4e6284f4d630a2d538d1eba2'},
 'ApiOrganizationFromHostedJobsPageName': {'digest': 'a346bea82800fd01f177b354e311e4da4e4492529d197a0de4c353e49aa53699',
                                           'variables': ['organizationHostedJobsPageName', 'searchContext'],
                                           'roots': ['organizationFromHostedJobsPageName'],
                                           'dispatch_digest': 'c1b33d9c40163b87066bb36b8051d6de3e3c920954762d2fdbeb42ee21e6f78a'}}
