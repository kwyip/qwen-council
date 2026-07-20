from __future__ import annotations

import os
import html
import re
import random
import time
import signal
import subprocess
import sys
from pathlib import Path
import sqlite3
import json
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any
from urllib.parse import quote, urlencode
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
import xml.etree.ElementTree as ET
from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from markupsafe import Markup

ARXIV_API_URL = "https://export.arxiv.org/api/query"
START_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)
END_DATE = datetime.now(timezone.utc)
DEFAULT_QUERY = "all:*"
ARTICLE_SORTS = {"new", "popular", "best"}
EST = timezone(timedelta(hours=-5), name="EST")
QUERY_OPERATORS = (" AND ", " OR ", " ANDNOT ")
ARXIV_ARCHIVES = {
    "physics": {
        "label": "Physics",
        "codes": ("astro-ph*", "cond-mat*", "gr-qc", "hep-ex", "hep-lat", "hep-ph", "hep-th", "math-ph", "nlin*", "nucl-ex", "nucl-th", "physics*", "quant-ph"),
    },
    "cs": {"label": "Computer Science", "codes": ("cs*",)},
    "math": {"label": "Mathematics", "codes": ("math*",)},
    "q-bio": {"label": "Quantitative Biology", "codes": ("q-bio*",)},
    "q-fin": {"label": "Quantitative Finance", "codes": ("q-fin*",)},
    "stat": {"label": "Statistics", "codes": ("stat*",)},
    "eess": {"label": "Electrical Engineering and Systems Science", "codes": ("eess*",)},
    "econ": {"label": "Economics", "codes": ("econ*",)},
}


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
        DATABASE=os.path.join(app.instance_path, "quantum_council.sqlite"),
        ARXIV_PAGE_SIZE=25,
        ARXIV_TIMEOUT=5,
        ADMIN_USERNAMES=os.environ.get("ADMIN_USERNAMES", "admin"),
        QWEN_TEXT_MODELS=os.environ.get("QWEN_TEXT_MODELS", "qwen3.7-plus,qwen-plus,qwen-turbo"),
        # Deprecated compatibility flag: reviewer deployment is admin-only and never automatic.
        AUTO_AGENT_REVIEWS=False,
    )
    if test_config:
        app.config.update(test_config)

    app.jinja_env.filters["markdown"] = render_markdown
    app.jinja_env.filters["est_time"] = format_est_time

    os.makedirs(app.instance_path, exist_ok=True)

    @app.before_request
    def load_logged_in_user() -> None:
        user_id = session.get("user_id")
        g.user = None
        if user_id is not None:
            g.user = query_db("SELECT id, username, profile_pic FROM users WHERE id = ?", (user_id,), one=True)

    @app.teardown_appcontext
    def close_db(_: BaseException | None = None) -> None:
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.route("/")
    @app.route("/<archive>")
    def index(archive: str | None = None):
        if archive is not None and archive not in ARXIV_ARCHIVES:
            abort(404)
        page = max(request.args.get("page", 1, type=int), 1)
        sort = request.args.get("sort", "new")
        if sort not in ARTICLE_SORTS:
            sort = "new"
        search = request.args.get("q", DEFAULT_QUERY).strip() or DEFAULT_QUERY
        try:
            articles, total = search_articles(search, page, app.config["ARXIV_PAGE_SIZE"], app.config["ARXIV_TIMEOUT"], archive, sort)
        except ArxivFetchError as error:
            flash(str(error))
            articles, total = cached_articles(DEFAULT_QUERY, page, app.config["ARXIV_PAGE_SIZE"], archive, sort)
            if articles:
                flash("Showing downloaded papers while arXiv is unavailable.")
        article_ids = [article["id"] for article in articles]
        if articles and app.config["AUTO_AGENT_REVIEWS"]:
            try:
                posted = deploy_agent_reviews_for_articles(articles)
            except AgentReviewUnavailable as error:
                flash(str(error))
            else:
                if posted:
                    flash(f"Reviewer agents posted {posted} new automatic comment{'s' if posted != 1 else ''}.")
        counts = comment_counts(article_ids)
        scores = vote_scores("article", article_ids)
        showcase_activity = recent_showcase_activity() if is_admin_user(g.user) else []
        return render_template(
            "index.html",
            archives=ARXIV_ARCHIVES,
            active_archive=archive,
            active_archive_label=ARXIV_ARCHIVES[archive]["label"] if archive else "All arXiv",
            articles=articles,
            counts=counts,
            scores=scores,
            is_admin=is_admin_user(g.user),
            reviewer_models=reviewer_model_options(),
            showcase_automation=showcase_automation_status() if is_admin_user(g.user) else None,
            showcase_activity=showcase_activity,
            page=page,
            page_size=app.config["ARXIV_PAGE_SIZE"],
            query=search,
            sort=sort,
            search_value="" if search == DEFAULT_QUERY else search,
            total=total,
            start_date=START_DATE,
            end_date=END_DATE,
            visit_time_est=datetime.now(EST),
        )

    @app.route("/article/<path:arxiv_id>", methods=("GET", "POST"))
    def article(arxiv_id: str):
        try:
            article_data = get_article(arxiv_id, app.config["ARXIV_TIMEOUT"])
        except ArxivFetchError:
            abort(502)
        if article_data is None:
            abort(404)
        if request.method == "POST":
            if g.user is None:
                flash("Please log in to comment.")
                return redirect(url_for("login", next=request.path))
            body = request.form.get("body", "").strip()
            parent_id = request.form.get("parent_id", type=int)
            if not body:
                flash("Comment cannot be empty.")
            elif parent_id is not None and not comment_belongs_to_article(parent_id, article_data["id"]):
                abort(400)
            else:
                db = get_db()
                db.execute(
                    "INSERT INTO comments (arxiv_id, user_id, body, parent_id) VALUES (?, ?, ?, ?)",
                    (article_data["id"], g.user["id"], body, parent_id),
                )
                db.commit()
                flash("Reply posted." if parent_id is not None else "Comment posted.")
                return redirect(url_for("article", arxiv_id=article_data["id"]))
        comments = query_db(
            """
            SELECT comments.id, comments.body, comments.parent_id, comments.created_at,
                   COALESCE(comments.display_name, users.username) AS username,
                   COALESCE(comments.display_profile_pic, users.profile_pic) AS profile_pic,
                   comments.reviewer_personality,
                   CASE WHEN users.username LIKE 'qwen-%' THEN 1 ELSE 0 END AS is_reviewer
            FROM comments JOIN users ON comments.user_id = users.id
            WHERE comments.arxiv_id = ?
            ORDER BY comments.created_at ASC
            """,
            (article_data["id"],),
        )
        comment_ids = [str(comment["id"]) for comment in comments]
        return render_template(
            "article.html",
            article=article_data,
            article_score=vote_score("article", article_data["id"]),
            comments=comment_threads(comments),
            comment_scores=vote_scores("comment", comment_ids),
            is_admin=is_admin_user(g.user),
            reviewer_agents=reviewer_agent_options(),
            reviewer_models=reviewer_model_options(),
        )

    @app.route("/register", methods=("GET", "POST"))
    def register():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            profile_pic = request.form.get("profile_pic", "").strip()
            if not username or not password:
                flash("Username and password are required.")
            else:
                try:
                    db = get_db()
                    db.execute(
                        "INSERT INTO users (username, password_hash, profile_pic) VALUES (?, ?, ?)",
                        (username, generate_password_hash(password), profile_pic),
                    )
                    db.commit()
                except sqlite3.IntegrityError:
                    flash("Username is already taken.")
                else:
                    flash("Registration complete. Please log in.")
                    return redirect(url_for("login"))
        return render_template("auth.html", mode="Register")

    @app.route("/login", methods=("GET", "POST"))
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = query_db("SELECT * FROM users WHERE username = ?", (username,), one=True)
            if user is None or not check_password_hash(user["password_hash"], password):
                flash("Invalid username or password.")
            else:
                session.clear()
                session["user_id"] = user["id"]
                return redirect(request.args.get("next") or url_for("index"))
        return render_template("auth.html", mode="Login")

    @app.route("/profile", methods=("GET", "POST"))
    @login_required
    def profile():
        if request.method == "POST":
            profile_pic = request.form.get("profile_pic", "").strip()
            db = get_db()
            db.execute("UPDATE users SET profile_pic = ? WHERE id = ?", (profile_pic, g.user["id"]))
            db.commit()
            flash("Profile updated.")
            return redirect(url_for("profile"))
        return render_template("profile.html")

    @app.route("/vote", methods=("POST",))
    def vote():
        if g.user is None:
            flash("Please log in to vote.")
            return redirect(url_for("login", next=request.form.get("next") or url_for("index")))
        target_type = request.form.get("target_type", "")
        target_id = request.form.get("target_id", "")
        value = request.form.get("value", type=int)
        next_url = request.form.get("next") or url_for("index")
        if target_type not in {"article", "comment"} or not target_id or value not in {-1, 1}:
            abort(400)
        db = get_db()
        existing = query_db(
            "SELECT id, value FROM votes WHERE user_id = ? AND target_type = ? AND target_id = ?",
            (g.user["id"], target_type, target_id),
            one=True,
        )
        if existing and existing["value"] == value:
            db.execute("DELETE FROM votes WHERE id = ?", (existing["id"],))
        elif existing:
            db.execute("UPDATE votes SET value = ? WHERE id = ?", (value, existing["id"]))
        else:
            db.execute(
                "INSERT INTO votes (user_id, target_type, target_id, value) VALUES (?, ?, ?, ?)",
                (g.user["id"], target_type, target_id, value),
            )
        db.commit()
        return redirect(next_url)

    @app.route("/admin/showcase-review", methods=("POST",))
    def admin_showcase_review():
        require_admin()
        try:
            article_id, agent_name, posted = run_showcase_review()
        except (AgentReviewUnavailable, ArxivFetchError, RuntimeError) as error:
            flash(str(error))
        else:
            paper_url = url_for("article", arxiv_id=article_id)
            flash(Markup(
                f"Showcase deployed {html.escape(agent_name)} on {html.escape(article_id)} "
                f"({posted} new comment). <a href=\"{html.escape(paper_url)}\">View paper and comments</a>."
            ))
        return redirect(request.form.get("next") or url_for("index"))

    @app.route("/admin/showcase-automation", methods=("POST",))
    def admin_showcase_automation():
        require_admin()
        action = request.form.get("action")
        if action == "start":
            interval = request.form.get("interval", type=int)
            automation_model = request.form.get("automation_model", "").strip() or None
            if not os.environ.get("DASHSCOPE_API_KEY"):
                flash("Set DASHSCOPE_API_KEY before starting autonomous reviews.")
            elif automation_model is not None and automation_model not in reviewer_model_options():
                flash("Select a configured Qwen text model or Random model.")
            elif interval is None or not 5 <= interval <= 86_400:
                flash("Choose an interval between 5 and 86400 seconds.")
            else:
                try:
                    started = start_showcase_automation(interval, automation_model)
                except OSError as error:
                    flash(f"Could not start autonomous showcase: {error}")
                else:
                    flash(
                        f"Autonomous showcase started with a {interval}-second interval."
                        if started else "Autonomous showcase is already running."
                    )
        elif action == "stop":
            flash("Autonomous showcase stopped." if stop_showcase_automation() else "Autonomous showcase was not running.")
        else:
            flash("The Start/Stop action was missing. Reload the page and try again.")
        return redirect(request.form.get("next") or url_for("index"))

    @app.route("/agent-reviews", methods=("POST",))
    def agent_reviews():
        if g.user is None:
            flash("Please log in as an administrator to deploy reviewer agents.")
            return redirect(url_for("login", next=request.form.get("next") or url_for("index")))
        if not is_admin_user(g.user):
            flash("Only administrators can deploy reviewer agents.")
            return redirect(request.form.get("next") or url_for("index"))
        ids = request.form.getlist("arxiv_id")
        agent_usernames = request.form.getlist("agent_username")
        model = request.form.get("model", "").strip() or None
        parent_comment_id = request.form.get("parent_comment_id", type=int)
        next_url = request.form.get("next") or url_for("index")
        if not ids:
            flash("No articles were available for reviewer agents.")
            return redirect(next_url)
        articles = [article for article in cached_articles_by_id(ids) if article is not None]
        try:
            posted = deploy_agent_reviews_for_articles(
                articles, agent_usernames or None, model, parent_comment_id
            )
        except AgentReviewUnavailable as error:
            flash(str(error))
        except RuntimeError as error:
            flash(str(error))
        else:
            flash(f"Reviewer agents posted {posted} new comment{'s' if posted != 1 else ''}.")
        return redirect(next_url)

    @app.route("/admin/comments/<int:comment_id>/delete", methods=("POST",))
    def admin_delete_comment(comment_id: int):
        require_admin()
        comment = query_db("SELECT arxiv_id FROM comments WHERE id = ?", (comment_id,), one=True)
        if comment is None:
            abort(404)
        delete_comment_tree(comment_id)
        flash("Comment and its replies removed.")
        return redirect(request.form.get("next") or url_for("article", arxiv_id=comment["arxiv_id"]))

    @app.route("/admin/articles/<path:arxiv_id>/delete", methods=("POST",))
    def admin_delete_article(arxiv_id: str):
        require_admin()
        delete_article(arxiv_id)
        flash(f"Paper {arxiv_id} removed from this site.")
        return redirect(request.form.get("next") or url_for("index"))

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.route("/pokemon-avatar/<path:filename>")
    def pokemon_avatar(filename: str):
        return send_from_directory(os.path.join(app.instance_path, "pokemon"), filename)

    @app.route("/blog")
    def blog():
        story = Path(__file__).with_name("BLOG.md").read_text(encoding="utf-8")
        return render_template("blog.html", story=story)

    @app.route("/reviewer/<path:display_name>")
    def reviewer_profile(display_name: str):
        comments = query_db(
            """SELECT comments.id, comments.arxiv_id, comments.body, comments.parent_id,
                      comments.created_at, comments.display_profile_pic, comments.reviewer_personality,
                      arxiv_articles.title
               FROM comments
               LEFT JOIN arxiv_articles ON arxiv_articles.id = comments.arxiv_id
               WHERE comments.display_name = ?
               ORDER BY comments.id DESC""",
            (display_name,),
        )
        if not comments:
            abort(404)
        return render_template("reviewer.html", display_name=display_name, comments=comments)

    app.cli.add_command(init_db_command)
    app.cli.add_command(create_admin_command)
    app.cli.add_command(showcase_agents_command)
    app.cli.add_command(pull_arxiv_command)
    with app.app_context():
        init_db()
    return app


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(current_app_config("DATABASE"), detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
    return g.db


def current_app_config(key: str) -> Any:
    from flask import current_app

    return current_app.config[key]


def query_db(query: str, args: tuple[Any, ...] = (), one: bool = False):
    cur = get_db().execute(query, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv


def init_db() -> None:
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            profile_pic TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            arxiv_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            parent_id INTEGER,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (parent_id) REFERENCES comments (id)
        );
        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            target_type TEXT NOT NULL CHECK (target_type IN ('article', 'comment')),
            target_id TEXT NOT NULL,
            value INTEGER NOT NULL CHECK (value IN (-1, 1)),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, target_type, target_id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        );
        CREATE TABLE IF NOT EXISTS arxiv_articles (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            authors TEXT NOT NULL,
            published TEXT NOT NULL,
            updated TEXT NOT NULL,
            primary_category TEXT NOT NULL,
            categories TEXT NOT NULL,
            cached_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS removed_articles (
            arxiv_id TEXT PRIMARY KEY,
            removed_by INTEGER NOT NULL,
            removed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (removed_by) REFERENCES users (id)
        );
        CREATE TABLE IF NOT EXISTS showcase_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            arxiv_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            model TEXT NOT NULL,
            comment_id INTEGER,
            posted_count INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    # CREATE TABLE IF NOT EXISTS does not add columns to an older table. Apply
    # column migrations before creating indexes that may refer to those columns.
    ensure_column(db, "users", "profile_pic", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "comments", "parent_id", "INTEGER")
    ensure_column(db, "comments", "display_name", "TEXT")
    ensure_column(db, "comments", "display_profile_pic", "TEXT")
    ensure_column(db, "comments", "reviewer_personality", "TEXT")
    db.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_comments_arxiv_id ON comments (arxiv_id);
        CREATE INDEX IF NOT EXISTS idx_comments_parent_id ON comments (parent_id);
        CREATE INDEX IF NOT EXISTS idx_votes_target ON votes (target_type, target_id);
        CREATE INDEX IF NOT EXISTS idx_arxiv_articles_published ON arxiv_articles (published DESC);
        CREATE INDEX IF NOT EXISTS idx_arxiv_articles_category ON arxiv_articles (primary_category);
        """
    )
    db.commit()


def ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


import click
from flask import current_app
from flask.cli import with_appcontext


@click.command("init-db")
@with_appcontext
def init_db_command() -> None:
    init_db()
    click.echo(f"Initialized database at {current_app.config['DATABASE']}.")


@click.command("create-admin")
@with_appcontext
@click.argument("username")
@click.password_option()
def create_admin_command(username: str, password: str) -> None:
    username = username.strip()
    if not username:
        raise click.ClickException("Username is required.")
    db = get_db()
    try:
        db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, generate_password_hash(password)),
        )
    except sqlite3.IntegrityError:
        db.execute("UPDATE users SET password_hash = ? WHERE username = ?", (generate_password_hash(password), username))
    db.commit()
    admins = {name.strip() for name in current_app.config["ADMIN_USERNAMES"].split(",") if name.strip()}
    if username not in admins:
        click.echo(f"Created login for {username}. Add ADMIN_USERNAMES=\"{username}\" (or include it in the comma-separated list) to grant administrator access.")
    else:
        click.echo(f"Administrator login ready for {username}.")


@click.command("showcase-agents")
@click.option("--interval", type=click.IntRange(min=5), default=30, show_default=True)
@click.option("--once", is_flag=True, help="Post one autonomous review and exit.")
@click.option("--model", default=None, help="Use one configured model instead of choosing randomly.")
@with_appcontext
def showcase_agents_command(interval: int, once: bool, model: str | None) -> None:
    """Continuously deploy random reviewers on random arXiv papers."""
    while True:
        try:
            article_id, agent_name, posted = run_showcase_review(model)
        except (AgentReviewUnavailable, ArxivFetchError, RuntimeError) as error:
            click.echo(f"Showcase review failed: {error}", err=True)
        else:
            click.echo(f"{agent_name} reviewed {article_id}: {posted} comment posted")
        if once:
            return
        time.sleep(interval)


@click.command("pull-arxiv")
@click.option("--pages", type=click.IntRange(min=1, max=20), default=2, show_default=True)
@click.option("--page-size", type=click.IntRange(min=1, max=200), default=100, show_default=True)
@with_appcontext
def pull_arxiv_command(pages: int, page_size: int) -> None:
    """Pull the latest arXiv metadata into the local cache."""
    pulled = 0
    for page in range(1, pages + 1):
        articles, _ = fetch_arxiv_articles(
            DEFAULT_QUERY, page, page_size, current_app.config["ARXIV_TIMEOUT"]
        )
        cache_articles(articles)
        pulled += len(articles)
    click.echo(f"Pulled {pulled} latest arXiv records into the local cache.")


def showcase_automation_files() -> tuple[str, str]:
    return (
        os.path.join(current_app.instance_path, "showcase-agents.pid"),
        os.path.join(current_app.instance_path, "showcase-agents.log"),
    )


def showcase_automation_status() -> dict[str, Any]:
    pid_file, log_file = showcase_automation_files()
    try:
        with open(pid_file, encoding="utf-8") as handle:
            pid = int(handle.read().strip())
        os.kill(pid, 0)
    except PermissionError:
        return {"running": True, "pid": pid, "log_file": log_file}
    except (FileNotFoundError, ValueError, ProcessLookupError, OSError):
        try:
            os.remove(pid_file)
        except FileNotFoundError:
            pass
        return {"running": False, "pid": None, "log_file": log_file}
    return {"running": True, "pid": pid, "log_file": log_file}


def start_showcase_automation(interval: int, model: str | None = None) -> bool:
    if showcase_automation_status()["running"]:
        return False
    pid_file, log_file = showcase_automation_files()
    os.makedirs(current_app.instance_path, exist_ok=True)
    log = open(log_file, "a", encoding="utf-8")
    try:
        command = [
            sys.executable, "-m", "flask", "--app", "app",
            "showcase-agents", "--interval", str(interval),
        ]
        if model:
            command.extend(["--model", model])
        process = subprocess.Popen(
            command,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log.close()
    with open(pid_file, "w", encoding="utf-8") as handle:
        handle.write(str(process.pid))
    return True


def stop_showcase_automation() -> bool:
    status = showcase_automation_status()
    if not status["running"]:
        return False
    pid_file, _ = showcase_automation_files()
    try:
        os.killpg(status["pid"], signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(status["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        os.remove(pid_file)
    except FileNotFoundError:
        pass
    return True


def is_admin_user(user: sqlite3.Row | None) -> bool:
    if user is None:
        return False
    usernames = {name.strip() for name in current_app_config("ADMIN_USERNAMES").split(",") if name.strip()}
    return user["username"] in usernames


def require_admin() -> None:
    if g.user is None or not is_admin_user(g.user):
        abort(403)


def delete_comment_tree(comment_id: int) -> None:
    db = get_db()
    rows = db.execute(
        """WITH RECURSIVE descendants(id) AS (
               SELECT id FROM comments WHERE id = ?
               UNION ALL
               SELECT comments.id FROM comments JOIN descendants ON comments.parent_id = descendants.id
           ) SELECT id FROM descendants""",
        (comment_id,),
    ).fetchall()
    ids = [str(row["id"]) for row in rows]
    if ids:
        placeholders = ",".join("?" for _ in ids)
        db.execute(f"DELETE FROM votes WHERE target_type = 'comment' AND target_id IN ({placeholders})", ids)
        db.execute(f"DELETE FROM comments WHERE id IN ({placeholders})", ids)
        db.commit()


def delete_article(arxiv_id: str) -> None:
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO removed_articles (arxiv_id, removed_by, removed_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
        (arxiv_id, g.user["id"]),
    )
    comment_ids = [str(row["id"]) for row in db.execute("SELECT id FROM comments WHERE arxiv_id = ?", (arxiv_id,))]
    if comment_ids:
        placeholders = ",".join("?" for _ in comment_ids)
        db.execute(f"DELETE FROM votes WHERE target_type = 'comment' AND target_id IN ({placeholders})", comment_ids)
    db.execute("DELETE FROM comments WHERE arxiv_id = ?", (arxiv_id,))
    db.execute("DELETE FROM votes WHERE target_type = 'article' AND target_id = ?", (arxiv_id,))
    db.execute("DELETE FROM arxiv_articles WHERE id = ?", (arxiv_id,))
    db.commit()


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        return view(**kwargs)

    return wrapped_view


class ArxivFetchError(RuntimeError):
    """Raised when arXiv cannot serve a feed request."""


def arxiv_date_query(query: str, archive: str | None = None, start_date: datetime = START_DATE, end_date: datetime = END_DATE) -> str:
    date_range = f"submittedDate:[{start_date:%Y%m%d%H%M} TO {end_date:%Y%m%d%H%M}]"
    normalized_query = normalize_arxiv_query(query)
    filters = [date_range]
    if normalized_query != DEFAULT_QUERY:
        filters.insert(0, f"({normalized_query})")
    if archive:
        filters.append(f"({archive_query(archive)})")
    return " AND ".join(filters)


def archive_query(archive: str) -> str:
    return " OR ".join(f"cat:{code}" for code in ARXIV_ARCHIVES[archive]["codes"])


def normalize_arxiv_query(query: str) -> str:
    query = " ".join(query.strip().split())
    if not query or query == DEFAULT_QUERY:
        return DEFAULT_QUERY
    if is_advanced_arxiv_query(query):
        return query
    terms = [escape_arxiv_term(term) for term in query.split()]
    return " AND ".join(f"all:{term}" for term in terms if term) or DEFAULT_QUERY


def escape_arxiv_term(term: str) -> str:
    return "".join(character for character in term if character.isalnum() or character in "-_.")


def is_advanced_arxiv_query(query: str) -> bool:
    return ":" in query or any(operator in query.upper() for operator in QUERY_OPERATORS)


def search_articles(
    query: str, page: int, page_size: int, timeout: int,
    archive: str | None = None, sort: str = "new",
) -> tuple[list[dict[str, Any]], int]:
    if query.strip() and query.strip() != DEFAULT_QUERY:
        return cached_articles(query, page, page_size, archive, sort)
    articles, _ = fetch_arxiv_articles(DEFAULT_QUERY, 1, page_size, timeout, archive)
    removed = removed_article_ids([article["id"] for article in articles])
    articles = [article for article in articles if article["id"] not in removed]
    cache_articles(articles)
    return cached_articles(DEFAULT_QUERY, page, page_size, archive, sort)


def fetch_arxiv_articles(
    query: str,
    page: int,
    page_size: int,
    timeout: int,
    archive: str | None = None,
    start_date: datetime = START_DATE,
    end_date: datetime = END_DATE,
) -> tuple[list[dict[str, Any]], int]:
    params = {
        "search_query": arxiv_date_query(query, archive, start_date, end_date),
        "start": (page - 1) * page_size,
        "max_results": page_size,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    feed = fetch_arxiv_feed(params, timeout)
    total_text = feed.findtext("{http://a9.com/-/spec/opensearch/1.1/}totalResults", "0")
    total = int(total_text or 0)
    entries = feed.findall("{http://www.w3.org/2005/Atom}entry")
    return [parse_entry(entry) for entry in entries], total


def get_article(arxiv_id: str, timeout: int) -> dict[str, Any] | None:
    if arxiv_id in removed_article_ids([arxiv_id]):
        return None
    cached = cached_articles_by_id([arxiv_id])
    if cached and cached[0] is not None:
        return cached[0]
    article = fetch_single_article(arxiv_id, timeout)
    if article is not None:
        cache_articles([article])
    return article


def fetch_single_article(arxiv_id: str, timeout: int) -> dict[str, Any] | None:
    feed = fetch_arxiv_feed({"id_list": arxiv_id}, timeout)
    entries = feed.findall("{http://www.w3.org/2005/Atom}entry")
    if not entries:
        return None
    return parse_entry(entries[0])


def fetch_arxiv_feed(params: dict[str, Any], timeout: int) -> ET.Element:
    url = f"{ARXIV_API_URL}?{urlencode(params)}"
    try:
        with urlopen(url, timeout=timeout) as response:
            return ET.fromstring(response.read())
    except HTTPError as error:
        raise ArxivFetchError(
            "arXiv could not process that search. Try a simpler keyword search or an arXiv field query such as all:quantum."
        ) from error
    except TimeoutError as error:
        raise ArxivFetchError("arXiv is taking too long to respond. Please try again or narrow your search.") from error
    except URLError as error:
        raise ArxivFetchError("Could not reach arXiv. Please try again in a moment.") from error
    except ET.ParseError as error:
        raise ArxivFetchError("arXiv returned an unreadable response. Please try again in a moment.") from error


def parse_entry(entry: ET.Element) -> dict[str, Any]:
    atom = "{http://www.w3.org/2005/Atom}"
    arxiv = "{http://arxiv.org/schemas/atom}"
    published = datetime.fromisoformat(required_text(entry, f"{atom}published").replace("Z", "+00:00"))
    updated = datetime.fromisoformat(required_text(entry, f"{atom}updated").replace("Z", "+00:00"))
    raw_id = required_text(entry, f"{atom}id")
    article_id = raw_id.rsplit("/", 1)[-1]
    primary = entry.find(f"{arxiv}primary_category")
    return {
        "id": article_id,
        "title": required_text(entry, f"{atom}title").replace("\n", " ").strip(),
        "summary": required_text(entry, f"{atom}summary").replace("\n", " ").strip(),
        "authors": [required_text(author, f"{atom}name") for author in entry.findall(f"{atom}author")],
        "published": published,
        "updated": updated,
        "pdf_url": f"https://arxiv.org/pdf/{quote(article_id)}",
        "abs_url": f"https://arxiv.org/abs/{quote(article_id)}",
        "primary_category": primary.attrib.get("term", "arXiv") if primary is not None else "arXiv",
        "categories": [category.attrib.get("term", "") for category in entry.findall(f"{atom}category")],
    }


def required_text(entry: ET.Element, name: str) -> str:
    value = entry.findtext(name)
    if value is None:
        raise ValueError(f"arXiv response is missing {name}")
    return value

def vote_score(target_type: str, target_id: str) -> int:
    rows = vote_scores(target_type, [target_id])
    return rows.get(target_id, 0)


def vote_scores(target_type: str, target_ids: list[str]) -> dict[str, int]:
    if not target_ids:
        return {}
    placeholders = ",".join("?" for _ in target_ids)
    rows = query_db(
        f"SELECT target_id, COALESCE(SUM(value), 0) AS score FROM votes WHERE target_type = ? AND target_id IN ({placeholders}) GROUP BY target_id",
        (target_type, *target_ids),
    )
    return {row["target_id"]: row["score"] for row in rows}


def comment_counts(arxiv_ids: list[str]) -> dict[str, int]:
    if not arxiv_ids:
        return {}
    placeholders = ",".join("?" for _ in arxiv_ids)
    rows = query_db(
        f"SELECT arxiv_id, COUNT(*) AS total FROM comments WHERE arxiv_id IN ({placeholders}) GROUP BY arxiv_id",
        tuple(arxiv_ids),
    )
    return {row["arxiv_id"]: row["total"] for row in rows}


def comment_belongs_to_article(comment_id: int, arxiv_id: str) -> bool:
    row = query_db("SELECT id FROM comments WHERE id = ? AND arxiv_id = ?", (comment_id, arxiv_id), one=True)
    return row is not None


def comment_threads(comments: list[sqlite3.Row]) -> list[dict[str, Any]]:
    by_id: dict[int, dict[str, Any]] = {}
    roots: list[dict[str, Any]] = []
    for comment in comments:
        item = dict(comment)
        item["replies"] = []
        by_id[item["id"]] = item
    for item in by_id.values():
        parent_id = item.get("parent_id")
        if parent_id in by_id:
            by_id[parent_id]["replies"].append(item)
        else:
            roots.append(item)
    return roots


def cache_articles(articles: list[dict[str, Any]]) -> None:
    if not articles:
        return
    db = get_db()
    removed = removed_article_ids([article["id"] for article in articles])
    articles = [article for article in articles if article["id"] not in removed]
    if not articles:
        return
    db.executemany(
        """
        INSERT INTO arxiv_articles (id, title, summary, authors, published, updated, primary_category, categories, cached_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title, summary=excluded.summary, authors=excluded.authors,
            published=excluded.published, updated=excluded.updated, primary_category=excluded.primary_category,
            categories=excluded.categories
        """,
        [
            (
                article["id"], article["title"], article["summary"], json.dumps(article["authors"]),
                article["published"].isoformat(), article["updated"].isoformat(), article["primary_category"], json.dumps(article["categories"]),
            )
            for article in articles
        ],
    )
    db.commit()


def cached_articles(
    query: str, page: int, page_size: int, archive: str | None = None, sort: str = "new"
) -> tuple[list[dict[str, Any]], int]:
    where = ["published BETWEEN ? AND ?", "id NOT IN (SELECT arxiv_id FROM removed_articles)"]
    args: list[Any] = [START_DATE.isoformat(), END_DATE.isoformat()]
    normalized = query.strip()
    if normalized and normalized != DEFAULT_QUERY:
        words = [escape_arxiv_term(word).lower() for word in normalized.split() if escape_arxiv_term(word)]
        for word in words:
            where.append("(lower(title) LIKE ? OR lower(summary) LIKE ? OR lower(authors) LIKE ? OR lower(primary_category) LIKE ?)")
            like = f"%{word}%"
            args.extend([like, like, like, like])
    if archive:
        codes = [code.rstrip("*") for code in ARXIV_ARCHIVES[archive]["codes"]]
        where.append("(" + " OR ".join("primary_category LIKE ?" for _ in codes) + ")")
        args.extend(f"{code}%" for code in codes)
    where_sql = " AND ".join(where)
    order_by = {
        "new": "cached_at DESC, published DESC",
        "popular": "(SELECT COUNT(*) FROM comments WHERE comments.arxiv_id = arxiv_articles.id) DESC, cached_at DESC",
        "best": "(SELECT COALESCE(SUM(value), 0) FROM votes WHERE votes.target_type = 'article' AND votes.target_id = arxiv_articles.id) DESC, cached_at DESC",
    }.get(sort, "cached_at DESC, published DESC")
    total = query_db(f"SELECT COUNT(*) AS total FROM arxiv_articles WHERE {where_sql}", tuple(args), one=True)["total"]
    rows = query_db(
        f"SELECT * FROM arxiv_articles WHERE {where_sql} ORDER BY {order_by} LIMIT ? OFFSET ?",
        (*args, page_size, (page - 1) * page_size),
    )
    return [row_to_article(row) for row in rows], total


def cached_articles_by_id(arxiv_ids: list[str]) -> list[dict[str, Any] | None]:
    if not arxiv_ids:
        return []
    placeholders = ",".join("?" for _ in arxiv_ids)
    rows = query_db(f"SELECT * FROM arxiv_articles WHERE id IN ({placeholders}) AND id NOT IN (SELECT arxiv_id FROM removed_articles)", tuple(arxiv_ids))
    by_id = {row["id"]: row_to_article(row) for row in rows}
    return [by_id.get(arxiv_id) for arxiv_id in arxiv_ids]


def removed_article_ids(arxiv_ids: list[str]) -> set[str]:
    if not arxiv_ids:
        return set()
    placeholders = ",".join("?" for _ in arxiv_ids)
    rows = query_db(f"SELECT arxiv_id FROM removed_articles WHERE arxiv_id IN ({placeholders})", tuple(arxiv_ids))
    return {row["arxiv_id"] for row in rows}


def row_to_article(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "summary": row["summary"],
        "authors": json.loads(row["authors"]),
        "published": datetime.fromisoformat(str(row["published"])),
        "updated": datetime.fromisoformat(str(row["updated"])),
        "pdf_url": f"https://arxiv.org/pdf/{quote(row['id'])}",
        "abs_url": f"https://arxiv.org/abs/{quote(row['id'])}",
        "primary_category": row["primary_category"],
        "categories": json.loads(row["categories"]),
    }


class AgentReviewUnavailable(RuntimeError):
    """Raised when reviewer agents cannot be run from a web request."""


def reviewer_agent_options() -> tuple[dict[str, str], ...]:
    """Return reviewer choices without requiring the optional OpenAI SDK import."""
    return (
        {"username": "qwen-easy-reviewer", "display": "Easy reviewer (accepting)"},
        {"username": "qwen-balanced-reviewer", "display": "Balanced reviewer"},
        {"username": "qwen-harsh-reviewer", "display": "Harsh reviewer"},
    )


def reviewer_model_options() -> tuple[str, ...]:
    return tuple(model.strip() for model in current_app_config("QWEN_TEXT_MODELS").split(",") if model.strip())


def deploy_agent_reviews_for_articles(
    articles: list[dict[str, Any]], agent_usernames: list[str] | None = None,
    model: str | None = None, parent_comment_id: int | None = None,
) -> int:
    if not articles:
        return 0
    if not os.environ.get("DASHSCOPE_API_KEY"):
        raise AgentReviewUnavailable("Set DASHSCOPE_API_KEY before deploying reviewer agents.")
    valid_usernames = {agent["username"] for agent in reviewer_agent_options()}
    if agent_usernames is not None and (
        not agent_usernames or any(username not in valid_usernames for username in agent_usernames)
    ):
        raise AgentReviewUnavailable("Select a valid reviewer agent.")
    if model is not None and model not in reviewer_model_options():
        raise AgentReviewUnavailable("Select a configured Qwen text model.")
    if parent_comment_id is not None:
        if len(articles) != 1 or not comment_belongs_to_article(parent_comment_id, articles[0]["id"]):
            raise AgentReviewUnavailable("The reply target does not belong to this paper.")
    from agent_reviews import review_articles

    return review_articles(articles, agent_usernames, model, parent_comment_id)


def run_showcase_review(model: str | None = None) -> tuple[str, str, int]:
    """Autonomously review a random paper or continue a human/AI discussion."""
    if not os.environ.get("DASHSCOPE_API_KEY"):
        raise AgentReviewUnavailable("Set DASHSCOPE_API_KEY before running the showcase.")
    if model is not None and model not in reviewer_model_options():
        raise AgentReviewUnavailable("Select a configured Qwen text model.")
    selected_model = model or random.choice(reviewer_model_options())
    target = autonomous_discussion_target() if random.random() < 0.7 else None
    agent_options = reviewer_agent_options()
    if target:
        article = cached_articles_by_id([target["arxiv_id"]])[0]
        if article is None:
            article = get_article(target["arxiv_id"], current_app_config("ARXIV_TIMEOUT"))
        articles = [article] if article else []
        agent = next(
            (option for option in agent_options if option["username"] == target["agent_username"]),
            random.choice(agent_options),
        )
        parent = target
    else:
        timeout = current_app_config("ARXIV_TIMEOUT")
        first, total = fetch_arxiv_articles(DEFAULT_QUERY, 1, 1, timeout)
        if not first or total < 1:
            raise ArxivFetchError("arXiv returned no papers for the showcase date range.")
        page = random.randint(1, min(total, 30_000))
        articles, _ = fetch_arxiv_articles(DEFAULT_QUERY, page, 1, timeout)
        removed = removed_article_ids([article["id"] for article in articles])
        articles = [article for article in articles if article["id"] not in removed]
        cache_articles(articles)
        agent = random.choice(agent_options)
        parent = query_db(
            "SELECT id FROM comments WHERE arxiv_id = ? ORDER BY RANDOM() LIMIT 1",
            (articles[0]["id"],), one=True,
        ) if articles and random.random() < 0.5 else None
    if not articles:
        raise ArxivFetchError("No available paper was found for this showcase run.")
    posted = deploy_agent_reviews_for_articles(
        articles, [agent["username"]], selected_model, parent["id"] if parent else None
    )
    comment = query_db(
        """SELECT comments.id FROM comments JOIN users ON comments.user_id = users.id
           WHERE comments.arxiv_id = ? AND users.username = ?
           ORDER BY comments.id DESC LIMIT 1""",
        (articles[0]["id"], agent["username"]), one=True,
    ) if posted else None
    db = get_db()
    db.execute(
        """INSERT INTO showcase_activity
           (arxiv_id, agent_name, model, comment_id, posted_count)
           VALUES (?, ?, ?, ?, ?)""",
        (articles[0]["id"], agent["display"], selected_model, comment["id"] if comment else None, posted),
    )
    db.commit()
    return articles[0]["id"], agent["display"], posted


def autonomous_discussion_target() -> sqlite3.Row | None:
    """Prefer a human reply that challenges an AI review, then any human comment."""
    target = query_db(
        """SELECT human.id, human.arxiv_id, ai_user.username AS agent_username
           FROM comments AS human
           JOIN users AS human_user ON human.user_id = human_user.id
           JOIN comments AS ai ON human.parent_id = ai.id
           JOIN users AS ai_user ON ai.user_id = ai_user.id
           WHERE human_user.username NOT LIKE 'qwen-%' AND ai_user.username LIKE 'qwen-%'
             AND human.arxiv_id NOT IN (SELECT arxiv_id FROM removed_articles)
             AND NOT EXISTS (
                 SELECT 1 FROM comments AS response JOIN users AS response_user ON response.user_id = response_user.id
                 WHERE response.parent_id = human.id AND response_user.username LIKE 'qwen-%'
             )
           ORDER BY CASE WHEN lower(human.body) LIKE '%disagree%'
                           OR lower(human.body) LIKE '%wrong%'
                           OR lower(human.body) LIKE '%not convinced%'
                         THEN 0 ELSE 1 END, RANDOM() LIMIT 1""",
        one=True,
    )
    if target:
        return target
    return query_db(
        """SELECT comments.id, comments.arxiv_id, NULL AS agent_username
           FROM comments JOIN users ON comments.user_id = users.id
           WHERE users.username NOT LIKE 'qwen-%'
             AND comments.arxiv_id NOT IN (SELECT arxiv_id FROM removed_articles)
             AND NOT EXISTS (
                 SELECT 1 FROM comments AS response JOIN users AS response_user ON response.user_id = response_user.id
                 WHERE response.parent_id = comments.id AND response_user.username LIKE 'qwen-%'
             )
           ORDER BY comments.id DESC LIMIT 1""",
        one=True,
    )


def recent_showcase_activity(limit: int = 20) -> list[sqlite3.Row]:
    return query_db(
        """SELECT arxiv_id, agent_name, model, comment_id, posted_count, created_at
           FROM showcase_activity ORDER BY id DESC LIMIT ?""",
        (limit,),
    )


def render_markdown(source: str) -> Markup:
    """Render a safe, deliberately small Markdown subset used by comments."""
    escaped = html.escape(source or "")
    blocks: list[str] = []

    def stash_math(match: re.Match[str]) -> str:
        """Keep TeX opaque while Markdown emphasis is expanded."""
        blocks.append(match.group(0))
        return f"\x00BLOCK{len(blocks) - 1}\x00"

    def stash_code(match: re.Match[str]) -> str:
        language = re.sub(r"[^a-zA-Z0-9_-]", "", match.group(1).strip())
        code = match.group(2).strip("\n")
        blocks.append(f'<pre><code class="language-{language}">{code}</code></pre>')
        return f"\x00BLOCK{len(blocks) - 1}\x00"

    escaped = re.sub(r"```([^\n]*)\n(.*?)```", stash_code, escaped, flags=re.DOTALL)
    # Markdown underscores and asterisks are meaningful inside TeX. Stashing
    # display math first prevents expressions such as T^{in}_r from being
    # rewritten as emphasis before KaTeX receives them in the browser.
    escaped = re.sub(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]", stash_math, escaped, flags=re.DOTALL)
    escaped = re.sub(r"(?<!\\)\$(?!\$)(.+?)(?<!\\)\$|\\\((.+?)\\\)", stash_math, escaped)
    escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(
        r"!\[([^\]]*)\]\(((?:/static/|https?://)[^\s)]+)\)",
        r'<figure><img src="\2" alt="\1" loading="lazy"><figcaption>\1</figcaption></figure>',
        escaped,
    )
    escaped = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2" rel="nofollow noopener">\1</a>', escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<!_)_([^_\n]+)_(?!_)", lambda m: f"<em>{m.group(1) or m.group(2)}</em>", escaped)

    rendered: list[str] = []
    for block in re.split(r"\n\s*\n", escaped.strip()):
        if re.fullmatch(r"\x00BLOCK\d+\x00", block):
            rendered.append(block)
            continue
        lines = block.splitlines()
        if lines and all(re.match(r"^[-*] ", line) for line in lines):
            rendered.append("<ul>" + "".join(f"<li>{line[2:]}</li>" for line in lines) + "</ul>")
        elif lines and all(re.match(r"^\d+\. ", line) for line in lines):
            rendered.append("<ol>" + "".join(f"<li>{re.sub(r'^\d+\. ', '', line)}</li>" for line in lines) + "</ol>")
        elif lines and lines[0].startswith("# "):
            rendered.append(f"<h3>{lines[0][2:]}</h3>" + "<br>".join(lines[1:]))
        elif lines and all(line.startswith("> ") for line in lines):
            rendered.append("<blockquote>" + "<br>".join(line[2:] for line in lines) + "</blockquote>")
        else:
            rendered.append("<p>" + "<br>".join(lines) + "</p>")
    result = "".join(rendered)
    for index, block in enumerate(blocks):
        result = result.replace(f"\x00BLOCK{index}\x00", block)
    return Markup(result)


def format_est_time(value: datetime | str | None) -> str:
    """Render SQLite UTC timestamps in the site's explicitly labeled EST zone."""
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(EST).strftime("%Y-%m-%d %H:%M:%S EST")


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
