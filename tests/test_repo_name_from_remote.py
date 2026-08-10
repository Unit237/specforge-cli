from spec_cli.git import github_full_name_from_remote_url, repo_name_from_remote_url


def test_repo_name_from_remote_url_github_https():
    assert (
        repo_name_from_remote_url("https://github.com/acme/billing-service.git")
        == "billing-service"
    )


def test_repo_name_from_remote_url_ssh_shorthand():
    assert repo_name_from_remote_url("git@github.com:acme/widget.git") == "widget"


def test_repo_name_from_remote_url_none():
    assert repo_name_from_remote_url(None) is None
    assert repo_name_from_remote_url("") is None


def test_github_full_name_from_https_and_ssh_origins():
    assert (
        github_full_name_from_remote_url(
            "https://github.com/Unit237/specforge.git"
        )
        == "Unit237/specforge"
    )
    assert (
        github_full_name_from_remote_url("git@github.com:bayocotjc/Crawl.git")
        == "bayocotjc/Crawl"
    )
    assert (
        github_full_name_from_remote_url(
            "ssh://git@github.com/Unit237/PromptCompression"
        )
        == "Unit237/PromptCompression"
    )


def test_github_full_name_rejects_other_hosts_and_extra_paths():
    assert github_full_name_from_remote_url("git@gitlab.com:acme/app.git") is None
    assert github_full_name_from_remote_url("https://github.com/acme") is None
    assert github_full_name_from_remote_url("https://github.com/a/b/issues") is None
