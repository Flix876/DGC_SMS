from flask import (
    render_template, redirect, url_for, flash, jsonify, request,
    current_app, Response, abort, send_file,
)
from flask_login import login_required, current_user
from datetime import datetime, timezone, date
import csv
import enum
import io
import json

from sqlalchemy import extract as sa_extract, func
from sqlalchemy.orm import joinedload
from flask_wtf.csrf import generate_csrf

from app import db
from app.main import main_bp
from app.models import (
    Sample, SampleAssignment, SampleHistory, Notification, User,
    Role, SampleStatus, AssignmentStatus, Setting, Branch, Permission,
    KpiTarget, KPI_METRICS, AUTO_ACTUAL_KEYS,
    NonWorkingDay, calculate_working_days, fetch_non_working_days, jamaica_now,
    DocumentVersion, BackDateRequest,
    fiscal_year_for_date, fiscal_quarter_for_date,
    fiscal_quarter_months, fiscal_year_date_range,
    SupportingDocument, ReviewHistory, AuditLog,
    user_roles, user_branches, user_permissions,
    CustomRole, custom_role_permissions, user_custom_roles,
    DeleteRequest, DirectMessage, ActingRole,
    Invoice, InvoiceItem, DropdownConfig,
)

REPORT_PER_PAGE = 25

# Resubmission type classification constants.
# Maps internal resubmission_type values (stored in DocumentVersion.resubmission_type)
# to human-readable display labels used in the Analyst Report filter UI.
RESUBMISSION_TYPES = [
    ('preliminary',  'Preliminary Review'),
    ('technical',    'Senior Chemist Review'),
    ('deputy',       'Deputy Review'),
    ('hod',          'HOD Review'),
    ('unspecified',  'Unspecified Review'),
]

# All workflow statuses that can be used to filter the Analyst Report.
# These map to SampleStatus enum values via their .name attribute.
WORKFLOW_STATUSES = [
    ('REGISTERED',            'Registered'),
    ('ASSIGNED',              'Assigned'),
    ('IN_PROGRESS',           'In Progress'),
    ('REPORT_SUBMITTED',      'Report Submitted'),
    ('UNDER_PRELIMINARY_REVIEW', 'Preliminary Review'),
    ('UNDER_TECHNICAL_REVIEW',   'Senior Chemist Review'),
    ('RETURNED',              'Returned for Correction'),
    ('ACCEPTED',              'Accepted'),
    ('DEPUTY_REVIEW',         'Deputy Review'),
    ('DEPUTY_RETURNED',       'Returned by Deputy'),
    ('CERTIFICATE_PREPARATION', 'Certificate Preparation'),
    ('HOD_REVIEW',            'HOD Review'),
    ('HOD_RETURNED',          'Returned by HOD'),
    ('CERTIFIED',             'Certified'),
    ('REJECTED',              'Rejected'),
    ('COMPLETED',             'Completed'),
]

# Default setting key stored in the Setting table.
ANALYST_REPORT_RESUB_TYPES_KEY = 'analyst_report_default_resubmission_types'

# QA Performance Summary — calculation method setting key.
# Valid values: 'sample' (count by unique sample) or 'test' (count by test assignment).
QA_PERFORMANCE_COUNT_BY_KEY = 'qa_performance_count_by'

# Standard preliminary review comment categories used for the dashboard breakdown.
# Each tuple is (list_of_keywords, display_label).  The same keyword lists are
# used by _qa_return_reason_summary; keeping them in one place avoids drift.
PRELIM_COMMENT_CATEGORIES = [
    (['calculat', 'arithmetic', 'math', 'formula'],             'Missing/incorrect calculations'),
    (['unit', 'measurement', 'mg', 'g/l', 'ppm', 'ppb', '%'],  'Incorrect units'),
    (['typo', 'spelling', 'grammatical', 'typograph', 'error in text'], 'Typographical errors'),
    (['incomplete', 'missing', 'omit', 'not filled', 'blank'],  'Incomplete fields'),
    (['reference', 'standard', 'spec', 'limit', 'criteria'],    'Incorrect reference/specification'),
]

import re as _re

# Matches a run of whitespace (regular space, tabs, newlines, and the
# non-breaking space \xa0 that rich-text/paste input sometimes introduces).
_WHITESPACE_RE = _re.compile(r'[\s\xa0]+')


def _normalize_comment_text(text):
    """Lower-case and collapse whitespace so keyword matching tolerates
    extra spaces, tabs, non-breaking spaces, or mixed capitalization
    without altering the meaning of the text being matched."""
    return _WHITESPACE_RE.sub(' ', text.lower()).strip()


def _any_keyword_in_text(keywords, combined):
    """Return True if any keyword is present in `combined` (already
    normalized via `_normalize_comment_text`).

    Keywords are intentionally partial word stems (e.g. 'calculat' so it
    matches "calculation"/"calculations"/"calculate"/"miscalculated"), so a
    plain substring check is used rather than strict whole-word matching,
    which would otherwise miss those legitimate variations.
    """
    return any(kw in combined for kw in keywords)


def _prefetch_tat_non_working_days(samples):
    """Return non-working dates across the full TAT span of *samples*."""
    tat_ranges = [
        (
            s.date_registered.date()
            if isinstance(s.date_registered, datetime)
            else s.date_registered,
            s.certified_at.date()
            if isinstance(s.certified_at, datetime)
            else s.certified_at,
        )
        for s in samples
        if s.certified_at and s.date_registered
    ]
    return (
        fetch_non_working_days(
            min(r[0] for r in tat_ranges),
            max(r[1] for r in tat_ranges),
        )
        if tat_ranges else set()
    )



def _get_default_resubmission_types():
    """Return the default resubmission type filter list from settings.

    Returns None to mean "all types" (no filter), or a list of type keys.
    """
    raw = Setting.get(ANALYST_REPORT_RESUB_TYPES_KEY, 'all')
    if not raw or raw == 'all':
        return None
    return [t.strip() for t in raw.split(',') if t.strip()]


def _current_fiscal_year():
    """Return the current fiscal year (April-March)."""
    return fiscal_year_for_date(jamaica_now())


def _available_fiscal_years():
    """Return sorted list of fiscal years with data, plus the current one."""
    from sqlalchemy import extract
    rows = db.session.query(
        extract('year', Sample.date_registered).label('yr'),
        extract('month', Sample.date_registered).label('mo'),
    ).distinct().all()
    fy_set = set()
    for r in rows:
        if r.yr and r.mo:
            if int(r.mo) >= 4:
                fy_set.add(int(r.yr))
            else:
                fy_set.add(int(r.yr) - 1)
    fy_set.add(_current_fiscal_year())
    return sorted(fy_set)


def _fiscal_year_filter(query, date_column, year, quarter=None):
    """Apply fiscal year (and optional quarter) filter to a query.
    Financial year: April 1 of `year` to March 31 of `year+1`.
    Q1=Apr-Jun, Q2=Jul-Sep, Q3=Oct-Dec, Q4=Jan-Mar."""
    start, end = fiscal_year_date_range(year, quarter if quarter in (1, 2, 3, 4) else None)
    return query.filter(date_column >= start, date_column <= end)


_CERTIFIED_STATUSES = (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)


def _apply_certified_quarter_filter(q, year, quarter, month=0):
    """Filter a sample query by certification date while carrying forward uncertified samples.

    Certified/Completed samples are shown in the fiscal period of *certified_at*.
    All other statuses (in-progress, under review, etc.) are always included so
    that pending work is carried forward across periods.
    """
    from sqlalchemy import or_, and_, extract as sa_extract

    # Determine the fiscal period boundaries
    if month and 1 <= month <= 12:
        fy_start, fy_end = fiscal_year_date_range(year, None)
    elif quarter in (1, 2, 3, 4):
        fy_start, fy_end = fiscal_year_date_range(year, quarter)
    else:
        fy_start, fy_end = fiscal_year_date_range(year, None)

    # Build the certified-within-period condition
    cert_conditions = [
        Sample.status.in_(_CERTIFIED_STATUSES),
        Sample.certified_at.isnot(None),
        Sample.certified_at >= fy_start,
        Sample.certified_at <= fy_end,
    ]
    if month and 1 <= month <= 12:
        cert_conditions.append(sa_extract('month', Sample.certified_at) == month)

    return q.filter(or_(
        and_(*cert_conditions),
        Sample.status.notin_(_CERTIFIED_STATUSES),
    ))


def _maybe_send_report_reminders():
    """Run report-date reminders at most once per calendar day.

    Uses the 'last_reminder_date' setting to avoid duplicate runs.
    """
    today_str = date.today().isoformat()
    if Setting.get('last_reminder_date') == today_str:
        return
    try:
        from app.notifications import send_report_date_reminders
        send_report_date_reminders()
        Setting.set('last_reminder_date', today_str)
        db.session.commit()
    except Exception:
        current_app.logger.exception('Failed to send report date reminders')


@main_bp.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    return redirect(url_for('auth.login'))


@main_bp.route('/dashboard')
@login_required
def dashboard():
    stats = {}

    # Trigger reminder check (at most once per day, stored in settings)
    _maybe_send_report_reminders()

    if current_user.has_role(Role.CHEMIST) and not current_user.has_any_role(Role.OFFICER, Role.SENIOR_CHEMIST, Role.DEPUTY, Role.HOD, Role.ADMIN):
        my_assignments = SampleAssignment.query.filter_by(
            chemist_id=current_user.id
        )
        stats['total_assignments'] = my_assignments.count()
        stats['pending'] = my_assignments.filter(
            SampleAssignment.status.in_([
                AssignmentStatus.ASSIGNED,
                AssignmentStatus.IN_PROGRESS,
                AssignmentStatus.RETURNED,
            ])
        ).count()
        stats['submitted'] = my_assignments.filter(
            SampleAssignment.status.in_([
                AssignmentStatus.REPORT_SUBMITTED,
                AssignmentStatus.UNDER_PRELIMINARY_REVIEW,
                AssignmentStatus.UNDER_TECHNICAL_REVIEW,
            ])
        ).count()
        stats['completed'] = my_assignments.filter(
            SampleAssignment.status.in_([
                AssignmentStatus.ACCEPTED,
                AssignmentStatus.COMPLETED,
            ])
        ).count()

    elif current_user.has_role(Role.OFFICER) and not current_user.has_any_role(Role.SENIOR_CHEMIST, Role.DEPUTY, Role.HOD, Role.ADMIN):
        my_samples = Sample.query.filter_by(uploaded_by=current_user.id)
        stats['total_samples'] = my_samples.count()
        stats['registered'] = my_samples.filter_by(
            status=SampleStatus.REGISTERED
        ).count()
        # Count all samples that have at least one assignment currently awaiting
        # preliminary review — not restricted to this officer's uploads because
        # officers can see (and act on) all samples in the sample list.
        _prelim_ids = db.select(SampleAssignment.sample_id).where(
            SampleAssignment.status == AssignmentStatus.REPORT_SUBMITTED
        ).distinct().scalar_subquery()
        stats['preliminary_review'] = Sample.query.filter(
            Sample.id.in_(_prelim_ids)
        ).count()
        stats['in_progress'] = my_samples.filter(
            Sample.status.in_([
                SampleStatus.ASSIGNED,
                SampleStatus.IN_PROGRESS,
                SampleStatus.UNDER_TECHNICAL_REVIEW,
            ])
        ).count()
        stats['completed'] = my_samples.filter(
            Sample.status.in_([
                SampleStatus.CERTIFIED,
                SampleStatus.COMPLETED,
            ])
        ).count()

    elif current_user.has_role(Role.DEPUTY) and not current_user.has_any_role(Role.HOD, Role.ADMIN):
        query = Sample.query
        stats['total_samples'] = query.count()
        stats['deputy_review'] = query.filter_by(
            status=SampleStatus.DEPUTY_REVIEW
        ).count()
        stats['certificate_prep'] = query.filter(
            Sample.status.in_([
                SampleStatus.CERTIFICATE_PREPARATION,
                SampleStatus.HOD_RETURNED,
            ])
        ).count()
        stats['completed'] = query.filter(
            Sample.status.in_([
                SampleStatus.CERTIFIED,
                SampleStatus.COMPLETED,
            ])
        ).count()

    elif current_user.has_role(Role.GOVT_CHEMIST_ASSISTANT) and not current_user.has_any_role(
            Role.OFFICER, Role.SENIOR_CHEMIST, Role.DEPUTY, Role.HOD, Role.ADMIN):
        query = Sample.query
        stats['total_samples'] = query.count()
        stats['documents_uploaded'] = SampleHistory.query.filter(
            SampleHistory.action == 'Supporting Document Uploaded',
            SampleHistory.performed_by == current_user.id,
        ).count()
        stats['completed'] = query.filter(
            Sample.status.in_([
                SampleStatus.CERTIFIED,
                SampleStatus.COMPLETED,
            ])
        ).count()

    else:
        # Branch heads, HOD, Admin
        query = Sample.query
        if current_user.branches and current_user.has_role(Role.SENIOR_CHEMIST):
            query = query.filter(Sample.sample_type.in_(current_user.branches))

        stats['total_samples'] = query.count()
        stats['awaiting_assignment'] = query.filter_by(
            status=SampleStatus.REGISTERED
        ).count()
        stats['reports_pending_review'] = SampleAssignment.query.filter(
            SampleAssignment.status.in_([
                AssignmentStatus.REPORT_SUBMITTED,
                AssignmentStatus.UNDER_TECHNICAL_REVIEW,
            ])
        ).count()
        stats['completed'] = query.filter(
            Sample.status.in_([
                SampleStatus.CERTIFIED,
                SampleStatus.COMPLETED,
            ])
        ).count()

    # Pharmaceutical samples awaiting chemist assignment
    _pharma_branches = [Branch.PHARMACEUTICAL, Branch.PHARMACEUTICAL_NR]
    unassigned_pharma_samples = []
    if current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD, Role.ADMIN):
        _unassigned_q = Sample.query.filter(
            Sample.sample_type.in_(_pharma_branches),
            Sample.status == SampleStatus.REGISTERED,
        )
        if current_user.has_role(Role.SENIOR_CHEMIST) and not current_user.has_any_role(Role.HOD, Role.ADMIN):
            if current_user.branches:
                _unassigned_q = _unassigned_q.filter(
                    Sample.sample_type.in_(current_user.branches)
                )
            else:
                _unassigned_q = _unassigned_q.filter(False)
        unassigned_pharma_samples = _unassigned_q.order_by(
            Sample.date_registered.asc()
        ).all()

    # Recent notifications
    notifications = Notification.query.filter_by(
        user_id=current_user.id
    ).order_by(Notification.created_at.desc()).limit(10).all()

    # Upcoming report deadlines (within 7 days)
    from datetime import timedelta
    today = date.today()
    terminal_statuses = [
        SampleStatus.CERTIFIED, SampleStatus.COMPLETED, SampleStatus.REJECTED,
    ]
    deadline_query = Sample.query.filter(
        Sample.expected_report_date.isnot(None),
        Sample.expected_report_date >= today,
        Sample.expected_report_date <= today + timedelta(days=7),
        Sample.status.notin_(terminal_statuses),
    ).order_by(Sample.expected_report_date.asc())

    if current_user.has_any_role(Role.SENIOR_CHEMIST, Role.DEPUTY, Role.HOD, Role.ADMIN):
        deadline_samples = deadline_query.limit(10).all()
    elif current_user.has_role(Role.CHEMIST):
        assigned_sample_ids = db.select(SampleAssignment.sample_id).where(
            SampleAssignment.chemist_id == current_user.id
        ).scalar_subquery()
        deadline_samples = deadline_query.filter(
            Sample.id.in_(assigned_sample_ids)
        ).limit(10).all()
    else:
        deadline_samples = []

    status_colors = {
        'Registered': 'secondary', 'Assigned': 'primary', 'In Progress': 'info',
        'Report Submitted': 'warning', 'Preliminary Review': 'warning',
        'Technical Review': 'warning', 'Returned for Correction': 'danger',
        'Accepted': 'success', 'Deputy Review': 'info',
        'Returned by Deputy': 'danger', 'Certificate Preparation': 'info',
        'HOD Review': 'info', 'Returned by HOD': 'danger',
    }
    upcoming_deadlines = []
    for s in deadline_samples:
        days_remaining = (s.expected_report_date - today).days
        upcoming_deadlines.append({
            'sample': s,
            'days_remaining': days_remaining,
            'status_color': status_colors.get(s.status.value, 'secondary'),
        })

    return render_template(
        'dashboard.html', stats=stats, notifications=notifications,
        upcoming_deadlines=upcoming_deadlines,
        unassigned_pharma_samples=unassigned_pharma_samples,
        today=today,
    )


@main_bp.route('/notifications')
@login_required
def notifications():
    page = request.args.get('page', 1, type=int)
    pagination = Notification.query.filter_by(
        user_id=current_user.id
    ).order_by(Notification.created_at.desc()).paginate(page=page, per_page=25, error_out=False)
    return render_template('notifications.html', notifications=pagination.items, pagination=pagination)


@main_bp.route('/notifications/<int:notif_id>/read', methods=['POST'])
@login_required
def mark_notification_read(notif_id):
    notif = db.get_or_404(Notification, notif_id)
    if notif.user_id != current_user.id:
        flash('Access denied.', 'danger')
        return redirect(url_for('main.notifications'))
    notif.is_read = True
    db.session.commit()
    if notif.link and notif.link.startswith('/'):
        return redirect(notif.link)
    return redirect(url_for('main.notifications'))


@main_bp.route('/notifications/read-all', methods=['POST'])
@login_required
def mark_all_notifications_read():
    updated = Notification.query.filter_by(
        user_id=current_user.id, is_read=False
    ).update({'is_read': True})
    db.session.commit()
    if updated:
        flash(f'{updated} notification{"s" if updated != 1 else ""} marked as read.', 'success')
    else:
        flash('No unread notifications.', 'info')
    return redirect(url_for('main.notifications'))


@main_bp.route('/api/notifications/unread-count')
@login_required
def unread_notification_count():
    count = Notification.query.filter_by(
        user_id=current_user.id, is_read=False
    ).count()
    return jsonify({'count': count})


@main_bp.route('/api/notifications/latest')
@login_required
def latest_notifications():
    """Return recent unread notifications for live preview."""
    notifs = Notification.query.filter_by(
        user_id=current_user.id, is_read=False
    ).order_by(Notification.created_at.desc()).limit(5).all()
    data = [
        {
            'id': n.id,
            'title': n.title,
            'message': n.message[:120] + ('...' if len(n.message) > 120 else ''),
            'link': n.link,
            'created_at': n.created_at.strftime('%d %b %Y %H:%M') if n.created_at else '',
        }
        for n in notifs
    ]
    return jsonify({'notifications': data})


@main_bp.route('/api/keep-alive')
@login_required
def keep_alive():
    """Lightweight endpoint that refreshes the session cookie."""
    return jsonify({'status': 'ok'})


# ---------------------------------------------------------------------------
# Sample & Test Assignment Dashboard Widget
# ---------------------------------------------------------------------------

# Roles that are allowed to see every analyst's workload and reassign work.
_ASSIGNMENT_SUPERVISOR_ROLES = (
    Role.OFFICER, Role.SENIOR_CHEMIST, Role.DEPUTY, Role.HOD, Role.ADMIN,
)

# AssignmentStatus buckets used for dashboard summary counts / overdue checks.
_ASSIGNMENT_COMPLETED_STATUSES = {AssignmentStatus.COMPLETED, AssignmentStatus.ACCEPTED}


def _is_assignment_supervisor(user):
    return user.has_any_role(*_ASSIGNMENT_SUPERVISOR_ROLES)


@main_bp.route('/assignments')
@login_required
def assignment_dashboard():
    """Serves the Sample & Test Assignment Dashboard widget.

    The widget is a standalone React SPA that fetches live data from the
    JSON endpoints below (see ``/api/assignments/*``); it renders a
    supervisor view (all analysts) or an analyst view (own work only)
    depending on the role passed in ``user_context``.
    """
    is_supervisor = _is_assignment_supervisor(current_user)
    user_role = 'supervisor' if is_supervisor else 'analyst'

    # User context passed to the widget for RBAC. IDs are stringified so
    # they compare equal to the string IDs returned by the JSON API below.
    user_context = {
        'id': str(current_user.id),
        'displayName': current_user.full_name or current_user.username,
        'role': user_role,
        'canManageAssignments': is_supervisor,
        'canViewSensitiveData': is_supervisor,
    }

    return render_template(
        'assignment_dashboard.html',
        user_context=user_context,
        csrf_token_value=generate_csrf(),
    )


def _assignment_priority(due_date, today):
    """Derive a Routine/Urgent/STAT priority from the assignment's due date.

    There is no dedicated priority column on ``SampleAssignment`` today, so
    this is computed from the real ``expected_completion`` date rather than
    fabricated: overdue work is most urgent, work due within 2 days is
    urgent, everything else is routine.
    """
    if not due_date:
        return 'Routine'
    days_left = (due_date - today).days
    if days_left < 0:
        return 'STAT'
    if days_left <= 2:
        return 'Urgent'
    return 'Routine'


def _assignment_is_overdue(assignment, today):
    return (
        assignment.expected_completion is not None
        and assignment.expected_completion < today
        and assignment.status not in _ASSIGNMENT_COMPLETED_STATUSES
    )


def _serialize_assignment_record(assignment, today, history_entries=None):
    """Serialize a ``SampleAssignment`` (with eager-loaded sample/chemist)
    into the JSON shape consumed by the Assignment Dashboard widget."""
    sample = assignment.sample
    chemist = assignment.chemist
    assigner = assignment.assigner
    priority = _assignment_priority(assignment.expected_completion, today)
    overdue = _assignment_is_overdue(assignment, today)
    status_label = assignment.status.value if assignment.status else None
    due_iso = (
        assignment.expected_completion.isoformat()
        if assignment.expected_completion else None
    )
    assigned_iso = (
        assignment.assigned_date.isoformat() if assignment.assigned_date else None
    )

    return {
        'assignment': {
            'id': str(assignment.id),
            'sampleId': str(assignment.sample_id),
            'testId': str(assignment.id),
            'analystId': str(assignment.chemist_id) if assignment.chemist_id else None,
            'assignedBy': str(assignment.assigned_by) if assignment.assigned_by else None,
            'assignedByName': assigner.full_name if assigner else None,
            'assignedDateTime': assigned_iso,
            'status': status_label,
            'dueDateTime': due_iso,
            'priority': priority,
            'overdue': overdue,
            'reassignmentReason': None,
        },
        'sample': {
            'id': str(sample.id),
            'accessionNumber': sample.lab_number,
            'sampleName': sample.sample_name,
            'sampleType': sample.sample_type.value if sample.sample_type else None,
            'location': sample.parish,
            'receivedDateTime': (
                sample.date_received.isoformat() if sample.date_received else None
            ),
            'status': sample.status.value if sample.status else None,
        },
        'test': {
            'id': str(assignment.id),
            'sampleId': str(assignment.sample_id),
            'testName': assignment.test_name,
            'testReference': assignment.test_reference,
            'dueDateTime': due_iso,
            'status': status_label,
            'priority': priority,
            'assignedAnalystId': str(assignment.chemist_id) if assignment.chemist_id else None,
            'assignedBy': str(assignment.assigned_by) if assignment.assigned_by else None,
            'assignedDateTime': assigned_iso,
            'completedDateTime': (
                assignment.date_completed.isoformat()
                if assignment.date_completed else None
            ),
            'workItemUrl': url_for(
                'samples.assignment_detail', assignment_id=assignment.id
            ),
        },
        'analyst': (
            {
                'id': str(chemist.id),
                'displayName': chemist.full_name,
                'department': chemist.branch_names,
                'activeStatus': 'Active' if chemist.is_active_user else 'Inactive',
            }
            if chemist else None
        ),
        'history': history_entries or [],
    }


def _assignment_history_for_samples(sample_ids):
    """Return {sample_id: [history entries]} for assignment-related events,
    fetched in a single query to avoid N+1 lookups."""
    if not sample_ids:
        return {}
    rows = SampleHistory.query.filter(
        SampleHistory.sample_id.in_(sample_ids),
        SampleHistory.action.ilike('%assign%'),
    ).order_by(SampleHistory.created_at.desc()).all()

    by_sample = {}
    for row in rows:
        by_sample.setdefault(row.sample_id, []).append({
            'id': str(row.id),
            'action': row.action,
            'details': row.details,
            'performedBy': str(row.performed_by) if row.performed_by else None,
            'performedDateTime': row.created_at.isoformat() if row.created_at else None,
        })
    return by_sample


@main_bp.route('/api/assignments/records')
@login_required
def api_assignment_records():
    """JSON feed of sample/test assignment records backing the Assignment
    Dashboard widget. Supervisors see every assignment; analysts only ever
    receive rows assigned to their own user ID (enforced here, not just in
    the client)."""
    is_supervisor = _is_assignment_supervisor(current_user)

    query = SampleAssignment.query.options(
        joinedload(SampleAssignment.sample),
        joinedload(SampleAssignment.chemist),
        joinedload(SampleAssignment.assigner),
    )
    if not is_supervisor:
        query = query.filter(SampleAssignment.chemist_id == current_user.id)

    assignments = query.order_by(SampleAssignment.assigned_date.desc()).all()

    today = jamaica_now().date()
    history_by_sample = _assignment_history_for_samples(
        [a.sample_id for a in assignments]
    )

    records = [
        _serialize_assignment_record(
            a, today, history_by_sample.get(a.sample_id, [])
        )
        for a in assignments
    ]
    return jsonify({'records': records})


def _analyst_users():
    """Return active users who hold the Chemist (analyst) role."""
    rows = db.session.execute(
        db.select(user_roles.c.user_id).where(user_roles.c.role == Role.CHEMIST)
    ).fetchall()
    ids = {row.user_id for row in rows}
    # Fall back to the legacy single-role column for older accounts.
    ids.update(u.id for u in User.query.filter(User.role == Role.CHEMIST).all())
    if not ids:
        return []
    return (
        User.query.filter(User.id.in_(ids), User.is_active_user.is_(True))
        .order_by(User.first_name, User.last_name)
        .all()
    )


@main_bp.route('/api/assignments/analysts')
@login_required
def api_assignment_analysts():
    """JSON feed of analysts with aggregate workload counts, used by the
    supervisor view and the reassignment picker."""
    is_supervisor = _is_assignment_supervisor(current_user)
    analysts = _analyst_users() if is_supervisor else [current_user]

    today = jamaica_now().date()

    # Single aggregate query for status counts per chemist (avoids N+1).
    status_counts = db.session.query(
        SampleAssignment.chemist_id, SampleAssignment.status,
        func.count(SampleAssignment.id),
    ).group_by(SampleAssignment.chemist_id, SampleAssignment.status).all()

    overdue_counts = dict(db.session.query(
        SampleAssignment.chemist_id, func.count(SampleAssignment.id),
    ).filter(
        SampleAssignment.expected_completion.isnot(None),
        SampleAssignment.expected_completion < today,
        SampleAssignment.status.notin_(_ASSIGNMENT_COMPLETED_STATUSES),
    ).group_by(SampleAssignment.chemist_id).all())

    workload_by_chemist = {}
    for chemist_id, status, count in status_counts:
        workload = workload_by_chemist.setdefault(
            chemist_id, {'total': 0, 'inProgress': 0, 'overdue': 0, 'completed': 0}
        )
        workload['total'] += count
        if status in _ASSIGNMENT_COMPLETED_STATUSES:
            workload['completed'] += count
        elif status != AssignmentStatus.REJECTED:
            workload['inProgress'] += count
    for chemist_id, count in overdue_counts.items():
        workload_by_chemist.setdefault(
            chemist_id, {'total': 0, 'inProgress': 0, 'overdue': 0, 'completed': 0}
        )['overdue'] = count

    data = [
        {
            'id': str(user.id),
            'displayName': user.full_name,
            'department': user.branch_names,
            'activeStatus': 'Active' if user.is_active_user else 'Inactive',
            'workload': workload_by_chemist.get(
                user.id, {'total': 0, 'inProgress': 0, 'overdue': 0, 'completed': 0}
            ),
        }
        for user in analysts
    ]
    return jsonify({'analysts': data})


@main_bp.route('/api/assignments/<int:assignment_id>/reassign', methods=['POST'])
@login_required
def api_reassign_assignment(assignment_id):
    """Reassigns a sample/test assignment to a different analyst.

    Only supervisors (Officer, Senior Chemist, Deputy, HOD, Admin) may
    reassign work. The reassignment is recorded to ``SampleHistory`` so it
    shows up in the widget's assignment history for that sample.
    """
    if not _is_assignment_supervisor(current_user):
        abort(403)

    assignment = db.get_or_404(SampleAssignment, assignment_id)
    payload = request.get_json(silent=True) or {}
    reason = (payload.get('reason') or '').strip()

    try:
        new_analyst_id = int(payload.get('newAnalystId'))
    except (TypeError, ValueError):
        return jsonify({'error': 'A valid analyst must be selected.'}), 400

    new_analyst = db.session.get(User, new_analyst_id)
    if not new_analyst or not new_analyst.has_role(Role.CHEMIST):
        return jsonify({'error': 'Analyst not found.'}), 404
    if not new_analyst.is_active_user:
        return jsonify({
            'error': f'{new_analyst.full_name} is not an active analyst.'
        }), 400

    old_chemist = (
        db.session.get(User, assignment.chemist_id)
        if assignment.chemist_id else None
    )
    if old_chemist and old_chemist.id == new_analyst.id:
        return jsonify({
            'error': f'{new_analyst.full_name} is already assigned to this test.'
        }), 400
    if old_chemist and not reason:
        return jsonify({'error': 'A reassignment reason is required.'}), 400

    assignment.chemist_id = new_analyst.id
    assignment.assigned_by = current_user.id
    assignment.assigned_date = jamaica_now()

    details = (
        f'{assignment.test_name} reassigned from '
        f'{old_chemist.full_name if old_chemist else "Unassigned"} to '
        f'{new_analyst.full_name}.'
    )
    if reason:
        details += f' Reason: {reason}'

    db.session.add(SampleHistory(
        sample_id=assignment.sample_id,
        action='Reassigned' if old_chemist else 'Assigned',
        details=details,
        performed_by=current_user.id,
        action_type='Assignment Change',
        object_affected='Assignment',
    ))
    db.session.commit()

    today = jamaica_now().date()
    history_by_sample = _assignment_history_for_samples([assignment.sample_id])
    record = _serialize_assignment_record(
        assignment, today, history_by_sample.get(assignment.sample_id, [])
    )
    return jsonify({'record': record})


# ---------------------------------------------------------------------------
# Quarterly KPI Dashboard
# ---------------------------------------------------------------------------

@main_bp.route('/kpi')
@login_required
def kpi():
    from sqlalchemy import extract, func

    year = request.args.get('year', type=int, default=_current_fiscal_year())
    sort_by = request.args.get('sort', 'quarter')
    sort_dir = request.args.get('dir', 'asc')

    available_years = _available_fiscal_years()

    # Quarterly stats using fiscal year quarters
    quarters_data = []
    fiscal_q_labels = {1: 'Q1 (Apr-Jun)', 2: 'Q2 (Jul-Sep)',
                       3: 'Q3 (Oct-Dec)', 4: 'Q4 (Jan-Mar)'}
    for q in range(1, 5):
        start, end = fiscal_year_date_range(year, q)

        base_q = Sample.query.filter(
            Sample.date_registered >= start,
            Sample.date_registered <= end,
        )
        total = base_q.count()
        certified = base_q.filter(
            Sample.status.in_([SampleStatus.CERTIFIED, SampleStatus.COMPLETED])
        ).count()
        in_progress = base_q.filter(
            Sample.status.notin_([
                SampleStatus.CERTIFIED, SampleStatus.COMPLETED,
                SampleStatus.REJECTED
            ])
        ).count()
        rejected = base_q.filter(
            Sample.status == SampleStatus.REJECTED
        ).count()

        # Turnaround: average working days from date_registered to certified_at
        avg_tat = None
        certified_samples = base_q.filter(
            Sample.status.in_([SampleStatus.CERTIFIED, SampleStatus.COMPLETED]),
            Sample.certified_at.isnot(None),
        ).all()
        if certified_samples:
            tat_ranges = [
                (
                    s.date_registered.date()
                    if isinstance(s.date_registered, datetime)
                    else s.date_registered,
                    s.certified_at.date()
                    if isinstance(s.certified_at, datetime)
                    else s.certified_at,
                )
                for s in certified_samples
                if s.certified_at and s.date_registered
            ]
            non_working = (
                fetch_non_working_days(
                    min(r[0] for r in tat_ranges),
                    max(r[1] for r in tat_ranges),
                )
                if tat_ranges else set()
            )
            days_list = []
            for s in certified_samples:
                if s.certified_at and s.date_registered:
                    delta_days = calculate_working_days(s.date_registered, s.certified_at, non_working)
                    days_list.append(delta_days) if delta_days is not None else None
            avg_tat = round(sum(days_list) / len(days_list), 1) if days_list else None

        # By branch
        by_branch = {}
        for branch in Branch:
            by_branch[branch.value] = base_q.filter(
                Sample.sample_type == branch
            ).count()

        quarters_data.append({
            'quarter': q,
            'label': fiscal_q_labels[q],
            'total': total,
            'certified': certified,
            'in_progress': in_progress,
            'rejected': rejected,
            'avg_tat': avg_tat,
            'by_branch': by_branch,
        })

    # Sorting
    sort_key = sort_by if sort_by in ('quarter', 'total', 'certified', 'in_progress', 'rejected', 'avg_tat') else 'quarter'
    reverse = (sort_dir == 'desc')
    quarters_data.sort(key=lambda x: (x[sort_key] is None, x[sort_key] if x[sort_key] is not None else 0), reverse=reverse)

    return render_template(
        'kpi.html',
        quarters_data=quarters_data,
        year=year,
        available_years=available_years,
        sort_by=sort_by,
        sort_dir=sort_dir,
        Branch=Branch,
    )


# ---------------------------------------------------------------------------
# KPI Report  (Target / Actual / Variance)
# ---------------------------------------------------------------------------

def _auto_actuals(year, quarter):
    """Return a dict of auto-computed KPI actual values for *year* / *quarter*.
    Uses fiscal year quarters (Q1=Apr-Jun, Q2=Jul-Sep, Q3=Oct-Dec, Q4=Jan-Mar)."""
    start, end = fiscal_year_date_range(year, quarter)

    def _base(branch_filter):
        q = Sample.query.filter(
            Sample.date_registered >= start,
            Sample.date_registered <= end,
        )
        if isinstance(branch_filter, (list, tuple)):
            q = q.filter(Sample.sample_type.in_(branch_filter))
        else:
            q = q.filter(Sample.sample_type == branch_filter)
        return q

    def _count(branch_filter):
        return _base(branch_filter).filter(
            Sample.status.in_([SampleStatus.CERTIFIED, SampleStatus.COMPLETED])
        ).count()

    def _avg_tat(branch_filter, alcohol_type_filter=None):
        q = _base(branch_filter).filter(
            Sample.status.in_([SampleStatus.CERTIFIED, SampleStatus.COMPLETED]),
            Sample.certified_at.isnot(None),
        )
        if alcohol_type_filter is not None:
            q = q.filter(Sample.alcohol_type == alcohol_type_filter)
        samples = q.all()
        non_working = _prefetch_tat_non_working_days(samples)
        days = [
            calculate_working_days(s.date_registered, s.certified_at, non_working)
            for s in samples
            if s.certified_at and s.date_registered
        ]
        days = [d for d in days if d is not None]
        return round(sum(days) / len(days), 1) if days else None

    def _count_out_of_spec(branch_filter):
        """Count samples that have at least one out-of-spec assignment."""
        from sqlalchemy import exists as sa_exists
        q = _base(branch_filter).filter(
            sa_exists().where(
                SampleAssignment.sample_id == Sample.id,
                SampleAssignment.out_of_spec.is_(True),
            )
        )
        return q.count()

    def _count_pharma_tests(branch_filter):
        """Count total pharmaceutical test assignments performed in the period."""
        sample_ids = [
            s.id for s in _base(branch_filter).all()
        ]
        if not sample_ids:
            return 0
        return SampleAssignment.query.filter(
            SampleAssignment.sample_id.in_(sample_ids),
            SampleAssignment.status.in_([
                AssignmentStatus.ACCEPTED,
                AssignmentStatus.COMPLETED,
                AssignmentStatus.REPORT_SUBMITTED,
                AssignmentStatus.UNDER_PRELIMINARY_REVIEW,
                AssignmentStatus.UNDER_TECHNICAL_REVIEW,
            ]),
        ).count()

    pharma_branches = [Branch.PHARMACEUTICAL, Branch.PHARMACEUTICAL_NR]
    return {
        'pharma_coas':                    _count(pharma_branches),
        'milk_coas':                      _count(Branch.FOOD_MILK),
        'toxicology_roas':                _count(Branch.TOXICOLOGY),
        'alcohol_coas':                   _count(Branch.FOOD_ALCOHOL),
        'avg_days_pharma_coa':            _avg_tat(pharma_branches),
        'avg_days_milk_coa':              _avg_tat(Branch.FOOD_MILK),
        'avg_days_toxicology_roa':        _avg_tat(Branch.TOXICOLOGY),
        'avg_days_alcohol_coa':           _avg_tat(Branch.FOOD_ALCOHOL),
        'avg_days_alcohol_determination': _avg_tat(Branch.FOOD_ALCOHOL,
                                                   'Alcohol Determination'),
        'avg_days_alcohol_denatured':     _avg_tat(Branch.FOOD_ALCOHOL,
                                                   'Denatured Alcohol (bitrex)'),
        'avg_days_alcohol_det_denatured': _avg_tat(Branch.FOOD_ALCOHOL,
                                                   'Alcohol Determination and Denatured'),
        'out_of_spec_pharma':             _count_out_of_spec(pharma_branches),
        'out_of_spec_milk':               _count_out_of_spec(Branch.FOOD_MILK),
        'out_of_spec_toxicology':         _count_out_of_spec(Branch.TOXICOLOGY),
        'out_of_spec_alcohol':            _count_out_of_spec(Branch.FOOD_ALCOHOL),
        # Feature 3 – total pharmaceutical tests performed (count of assignments)
        'pharma_tests_performed':         _count_pharma_tests(pharma_branches),
    }


def _out_of_spec_count_for_samples(sample_ids):
    """Return the number of distinct samples (from *sample_ids*) that have at
    least one out-of-spec assignment."""
    if not sample_ids:
        return 0
    from sqlalchemy import distinct as sa_distinct
    return db.session.query(
        sa_distinct(SampleAssignment.sample_id)
    ).filter(
        SampleAssignment.sample_id.in_(sample_ids),
        SampleAssignment.out_of_spec.is_(True),
    ).count()


def _resubmission_counts_for_samples(sample_ids, review_types=None):
    """Return a dict of {sample_id: resubmission_count} for the given samples.

    Counts DocumentVersion rows with document_type='report' and
    upload_label='resubmission', which are created every time a chemist
    resubmits a report after it has been returned for correction.

    If *review_types* is provided (a list of resubmission_type strings),
    only resubmissions of those types are counted.  Pass None to count all.
    When 'unspecified' is in *review_types*, rows with a NULL resubmission_type
    are also included (they represent legacy resubmissions with no type set).
    """
    if not sample_ids:
        return {}
    from sqlalchemy import func, or_
    q = db.session.query(
        DocumentVersion.sample_id,
        func.count(DocumentVersion.id),
    ).filter(
        DocumentVersion.sample_id.in_(sample_ids),
        DocumentVersion.document_type == 'report',
        DocumentVersion.upload_label == 'resubmission',
    )
    if review_types is not None:
        if 'unspecified' in review_types:
            q = q.filter(
                or_(
                    DocumentVersion.resubmission_type.in_(review_types),
                    DocumentVersion.resubmission_type.is_(None),
                )
            )
        else:
            q = q.filter(DocumentVersion.resubmission_type.in_(review_types))
    rows = q.group_by(DocumentVersion.sample_id).all()
    return {sid: cnt for sid, cnt in rows}


def _resubmission_counts_for_assignments(assignment_ids, review_types=None):
    """Return a dict of {assignment_id: resubmission_count} for the given assignments.

    Counts DocumentVersion rows with document_type='report' and
    upload_label='resubmission' linked to each assignment_id.

    If *review_types* is provided (a list of resubmission_type strings),
    only resubmissions of those types are counted.  Pass None to count all.
    When 'unspecified' is in *review_types*, rows with a NULL resubmission_type
    are also included (they represent legacy resubmissions with no type set).
    """
    if not assignment_ids:
        return {}
    from sqlalchemy import func, or_
    q = db.session.query(
        DocumentVersion.assignment_id,
        func.count(DocumentVersion.id),
    ).filter(
        DocumentVersion.assignment_id.in_(assignment_ids),
        DocumentVersion.document_type == 'report',
        DocumentVersion.upload_label == 'resubmission',
    )
    if review_types is not None:
        if 'unspecified' in review_types:
            q = q.filter(
                or_(
                    DocumentVersion.resubmission_type.in_(review_types),
                    DocumentVersion.resubmission_type.is_(None),
                )
            )
        else:
            q = q.filter(DocumentVersion.resubmission_type.in_(review_types))
    rows = q.group_by(DocumentVersion.assignment_id).all()
    return {aid: cnt for aid, cnt in rows}


def _resubmission_type_breakdown_for_assignments(assignment_ids):
    """Return a dict of {assignment_id: {type_key: count}} for the given assignments.

    Counts every DocumentVersion row with document_type='report' and
    upload_label='resubmission', grouped by both assignment_id and
    resubmission_type.  A NULL resubmission_type (legacy data) is mapped
    to the key 'unspecified'.
    """
    if not assignment_ids:
        return {}
    from sqlalchemy import func
    rows = db.session.query(
        DocumentVersion.assignment_id,
        DocumentVersion.resubmission_type,
        func.count(DocumentVersion.id),
    ).filter(
        DocumentVersion.assignment_id.in_(assignment_ids),
        DocumentVersion.document_type == 'report',
        DocumentVersion.upload_label == 'resubmission',
    ).group_by(
        DocumentVersion.assignment_id, DocumentVersion.resubmission_type
    ).all()
    result = {}
    for aid, rtype, cnt in rows:
        type_key = rtype if rtype else 'unspecified'
        if aid not in result:
            result[aid] = {}
        result[aid][type_key] = result[aid].get(type_key, 0) + cnt
    return result


def _preliminary_return_counts_for_assignments(assignment_ids):
    """Return a dict of {assignment_id: return_count} for the given assignments.

    Counts distinct ReviewHistory rows with review_type='preliminary' and
    action='returned'.  Uses the authoritative return-event
    records rather than DocumentVersion resubmissions so that:
      - samples currently in a returned state (not yet resubmitted) are included;
      - the same event is never counted twice regardless of how many report rows
        reference the same assignment.
    """
    if not assignment_ids:
        return {}
    from sqlalchemy import func
    rows = (
        db.session.query(
            ReviewHistory.assignment_id,
            func.count(ReviewHistory.id),
        )
        .filter(
            ReviewHistory.assignment_id.in_(assignment_ids),
            ReviewHistory.review_type == 'preliminary',
            ReviewHistory.action == 'returned',
        )
        .group_by(ReviewHistory.assignment_id)
        .all()
    )
    return {aid: cnt for aid, cnt in rows}


@main_bp.route('/kpi/report')
@login_required
def kpi_report():
    """KPI Target vs Actual report (quarterly)."""
    # Explicit KPI_VIEW permission overrides role restrictions
    # (explicit user grants are evaluated before role-based access).
    if not (current_user.has_permission(Permission.KPI_VIEW)
            or current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                         Role.DEPUTY, Role.ADMIN)):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=1)
    if quarter not in (1, 2, 3, 4):
        quarter = 1

    # Available years (fiscal years from sample data + any year that has KPI targets)
    available_years = _available_fiscal_years()
    target_years = {
        r.year for r in db.session.query(KpiTarget.year).distinct().all()
    }
    available_years = sorted(set(available_years) | target_years | {year})

    # Load saved targets for this year/quarter
    targets = {
        t.kpi_key: t
        for t in KpiTarget.query.filter_by(year=year, quarter=quarter).all()
    }

    # Auto-computed actual values
    auto = _auto_actuals(year, quarter)

    # Build report rows
    rows = []
    for key, label in KPI_METRICS:
        t_obj = targets.get(key)
        target_val = t_obj.target_value if t_obj else None
        if key in AUTO_ACTUAL_KEYS:
            actual_val = auto.get(key)
        else:
            actual_val = t_obj.actual_override if t_obj else None

        if target_val is not None and actual_val is not None:
            variance = round(actual_val - target_val, 2)
        else:
            variance = None

        rows.append({
            'key': key,
            'label': label,
            'target': target_val,
            'actual': actual_val,
            'variance': variance,
        })

    return render_template(
        'kpi_report.html',
        rows=rows,
        year=year,
        quarter=quarter,
        available_years=available_years,
    )


@main_bp.route('/kpi/report/download')
@login_required
def kpi_report_download():
    """Download the KPI report as CSV."""
    # Explicit KPI_VIEW permission overrides role restrictions
    # (explicit user grants are evaluated before role-based access).
    if not (current_user.has_permission(Permission.KPI_VIEW)
            or current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                         Role.DEPUTY, Role.ADMIN)):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=jamaica_now().year)
    quarter = request.args.get('quarter', type=int, default=1)
    if quarter not in (1, 2, 3, 4):
        quarter = 1

    targets = {
        t.kpi_key: t
        for t in KpiTarget.query.filter_by(year=year, quarter=quarter).all()
    }
    auto = _auto_actuals(year, quarter)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['KPI', 'Target', 'Actual', 'Variance'])
    for key, label in KPI_METRICS:
        t_obj = targets.get(key)
        target_val = t_obj.target_value if t_obj else ''
        if key in AUTO_ACTUAL_KEYS:
            actual_val = auto.get(key)
            actual_val = actual_val if actual_val is not None else ''
        else:
            actual_val = t_obj.actual_override if t_obj and t_obj.actual_override is not None else ''

        if target_val != '' and actual_val != '':
            variance = round(float(actual_val) - float(target_val), 2)
        else:
            variance = ''
        writer.writerow([label, target_val, actual_val, variance])

    filename = f'KPI_Report_{year}_Q{quarter}.csv'
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# KPI Targets Management  (Admin / HOD)
# ---------------------------------------------------------------------------

@main_bp.route('/kpi/targets', methods=['GET', 'POST'])
@login_required
def kpi_targets():
    """Set KPI targets and manual actuals for a given year/quarter."""
    if not current_user.has_any_role(Role.HOD, Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=1)
    if quarter not in (1, 2, 3, 4):
        quarter = 1

    if request.method == 'POST':
        try:
            year = int(request.form.get('year', year))
            quarter = int(request.form.get('quarter', quarter))
        except (ValueError, TypeError):
            flash('Invalid year or quarter.', 'danger')
            return redirect(url_for('main.kpi_targets'))
        if quarter not in (1, 2, 3, 4):
            quarter = 1
        for key, _label in KPI_METRICS:
            target_raw = request.form.get(f'target_{key}', '').strip()
            actual_raw = request.form.get(f'actual_{key}', '').strip()

            try:
                target_val = float(target_raw) if target_raw else None
                actual_val = float(actual_raw) if actual_raw else None
            except (ValueError, TypeError):
                continue  # skip malformed values

            existing = KpiTarget.query.filter_by(
                year=year, quarter=quarter, kpi_key=key
            ).first()
            if existing:
                existing.target_value = target_val
                existing.actual_override = actual_val
            else:
                db.session.add(KpiTarget(
                    year=year, quarter=quarter, kpi_key=key,
                    target_value=target_val, actual_override=actual_val,
                ))
        db.session.commit()
        flash('KPI targets saved.', 'success')
        return redirect(url_for('main.kpi_targets', year=year, quarter=quarter))

    targets = {
        t.kpi_key: t
        for t in KpiTarget.query.filter_by(year=year, quarter=quarter).all()
    }

    # Available years (fiscal years)
    available_years = _available_fiscal_years()
    target_years = {
        r.year for r in db.session.query(KpiTarget.year).distinct().all()
    }
    fy = _current_fiscal_year()
    available_years = sorted(
        set(available_years) | target_years | {fy, fy + 1}
    )

    return render_template(
        'kpi_targets.html',
        kpi_metrics=KPI_METRICS,
        auto_keys=AUTO_ACTUAL_KEYS,
        targets=targets,
        year=year,
        quarter=quarter,
        available_years=available_years,
    )


# ---------------------------------------------------------------------------
# Pharmaceutical Reports
# ---------------------------------------------------------------------------

@main_bp.route('/reports/pharma')
@login_required
def pharma_report():
    """Pharmaceutical sample report with filtering and download."""
    # Explicit KPI_VIEW permission overrides role restrictions
    # (explicit user grants are evaluated before role-based access).
    if not (current_user.has_permission(Permission.KPI_VIEW)
            or current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                         Role.DEPUTY, Role.ADMIN)):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)  # 0 = all
    month = request.args.get('month', type=int, default=0)       # 0 = all (Feature 8)
    status_filter = request.args.get('status', '')
    formulation_filter = request.args.get('formulation_type', '').strip()
    api_filter = request.args.get('api', '').strip()
    source_filter = request.args.get('source', '').strip()
    manufacturer_filter = request.args.get('manufacturer', '').strip()

    q = Sample.query.filter(
        Sample.sample_type.in_([Branch.PHARMACEUTICAL, Branch.PHARMACEUTICAL_NR]),
    )
    # Certified samples shown by certification date; uncertified carried forward
    q = _apply_certified_quarter_filter(q, year, quarter, month)

    if status_filter:
        try:
            st = SampleStatus(status_filter)
            q = q.filter(Sample.status == st)
        except ValueError:
            pass

    if formulation_filter:
        q = q.filter(Sample.formulation_type.ilike(f'%{formulation_filter}%'))

    if api_filter:
        q = q.filter(Sample.api.ilike(f'%{api_filter}%'))

    if source_filter:
        q = q.filter(Sample.source.ilike(f'%{source_filter}%'))

    if manufacturer_filter:
        q = q.filter(Sample.manufacturer.ilike(f'%{manufacturer_filter}%'))

    samples = q.order_by(Sample.date_registered.desc()).all()

    # Summary stats
    total = len(samples)
    certified = sum(
        1 for s in samples
        if s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)
    )
    in_progress = sum(
        1 for s in samples
        if s.status not in (
            SampleStatus.CERTIFIED, SampleStatus.COMPLETED,
            SampleStatus.REJECTED,
        )
    )
    rejected = sum(1 for s in samples if s.status == SampleStatus.REJECTED)

    non_working = _prefetch_tat_non_working_days(samples)

    # Per-sample TAT (Feature 2)
    sample_tat = {}
    for s in samples:
        if s.certified_at and s.date_registered and s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED):
            sample_tat[s.id] = calculate_working_days(s.date_registered, s.certified_at, non_working)
        else:
            sample_tat[s.id] = None

    tat_days = [v for v in sample_tat.values() if v is not None]
    avg_tat = round(sum(tat_days) / len(tat_days), 1) if tat_days else None

    # Out-of-spec count
    sample_ids = [s.id for s in samples]
    out_of_spec_count = _out_of_spec_count_for_samples(sample_ids)

    # Resubmission counts per sample
    sample_resubmissions = _resubmission_counts_for_samples(sample_ids)

    # Available years (fiscal)
    available_years = _available_fiscal_years()

    # Pagination
    page = request.args.get('page', 1, type=int)
    total_pages = max(1, (total + REPORT_PER_PAGE - 1) // REPORT_PER_PAGE)
    page = max(1, min(page, total_pages))
    page_start = (page - 1) * REPORT_PER_PAGE
    page_samples = samples[page_start:page_start + REPORT_PER_PAGE]

    return render_template(
        'pharma_report.html',
        samples=page_samples,
        year=year,
        quarter=quarter,
        month=month,
        status_filter=status_filter,
        formulation_filter=formulation_filter,
        api_filter=api_filter,
        source_filter=source_filter,
        manufacturer_filter=manufacturer_filter,
        available_years=available_years,
        total=total,
        certified=certified,
        in_progress=in_progress,
        rejected=rejected,
        avg_tat=avg_tat,
        out_of_spec_count=out_of_spec_count,
        sample_tat=sample_tat,
        sample_resubmissions=sample_resubmissions,
        SampleStatus=SampleStatus,
        page=page,
        total_pages=total_pages,
    )


@main_bp.route('/reports/pharma/download')
@login_required
def pharma_report_download():
    """Download pharmaceutical report as CSV."""
    # Explicit KPI_VIEW permission overrides role restrictions
    # (explicit user grants are evaluated before role-based access).
    if not (current_user.has_permission(Permission.KPI_VIEW)
            or current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                         Role.DEPUTY, Role.ADMIN)):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)
    month = request.args.get('month', type=int, default=0)
    status_filter = request.args.get('status', '')
    formulation_filter = request.args.get('formulation_type', '').strip()
    api_filter = request.args.get('api', '').strip()
    source_filter = request.args.get('source', '').strip()
    manufacturer_filter = request.args.get('manufacturer', '').strip()

    q = Sample.query.filter(
        Sample.sample_type.in_([Branch.PHARMACEUTICAL, Branch.PHARMACEUTICAL_NR]),
    )
    q = _apply_certified_quarter_filter(q, year, quarter, month)

    if status_filter:
        try:
            q = q.filter(Sample.status == SampleStatus(status_filter))
        except ValueError:
            pass

    if formulation_filter:
        q = q.filter(Sample.formulation_type.ilike(f'%{formulation_filter}%'))

    if api_filter:
        q = q.filter(Sample.api.ilike(f'%{api_filter}%'))

    if source_filter:
        q = q.filter(Sample.source.ilike(f'%{source_filter}%'))

    if manufacturer_filter:
        q = q.filter(Sample.manufacturer.ilike(f'%{manufacturer_filter}%'))

    samples = q.order_by(Sample.date_registered.desc()).all()

    tat_ranges = [
        (
            s.date_registered.date()
            if isinstance(s.date_registered, datetime)
            else s.date_registered,
            s.certified_at.date()
            if isinstance(s.certified_at, datetime)
            else s.certified_at,
        )
        for s in samples
        if (
            s.certified_at and s.date_registered
            and s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)
        )
    ]
    non_working = (
        fetch_non_working_days(
            min(r[0] for r in tat_ranges),
            max(r[1] for r in tat_ranges),
        )
        if tat_ranges else set()
    )
    sample_ids = [s.id for s in samples]
    resubmissions = _resubmission_counts_for_samples(sample_ids)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        'Lab Number', 'Sample Name', 'Type', 'Formulation', 'Manufacturer', 'API',
        'Status', 'Date Received', 'Date Registered',
        'Certified Date', 'Turnaround (days)',
        'Report Resubmissions', 'COA Version',
    ])
    for s in samples:
        tat = ''
        if (s.certified_at and s.date_registered
                and s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)):
            tat = calculate_working_days(s.date_registered, s.certified_at, non_working)
        writer.writerow([
            s.lab_number,
            s.sample_name,
            s.sample_type.value if s.sample_type else '',
            s.formulation_type or '',
            s.manufacturer or '',
            s.api or '',
            s.status.value if s.status else '',
            s.date_received.isoformat() if s.date_received else '',
            s.date_registered.strftime('%Y-%m-%d') if s.date_registered else '',
            s.certified_at.strftime('%Y-%m-%d') if s.certified_at else '',
            tat,
            resubmissions.get(s.id, 0),
            s.coa_version if s.coa_version else 1,
        ])

    q_label = f'_Q{quarter}' if quarter in (1, 2, 3, 4) else (f'_M{month}' if month else '')
    filename = f'Pharmaceutical_Report_{year}{q_label}.csv'
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Milk Report
# ---------------------------------------------------------------------------

@main_bp.route('/reports/milk')
@login_required
def milk_report():
    """Milk sample report with filtering and download."""
    # Explicit KPI_VIEW permission overrides role restrictions
    # (explicit user grants are evaluated before role-based access).
    if not (current_user.has_permission(Permission.KPI_VIEW)
            or current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                         Role.DEPUTY, Role.ADMIN)):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)  # 0 = all
    month = request.args.get('month', type=int, default=0)
    status_filter = request.args.get('status', '')
    parish_filter = request.args.get('parish', '').strip()
    milk_type_filter = request.args.get('milk_type', '').strip()

    q = Sample.query.filter(
        Sample.sample_type == Branch.FOOD_MILK,
    )
    q = _apply_certified_quarter_filter(q, year, quarter, month)

    if status_filter:
        try:
            st = SampleStatus(status_filter)
            q = q.filter(Sample.status == st)
        except ValueError:
            pass

    if parish_filter:
        q = q.filter(Sample.parish.ilike(f'%{parish_filter}%'))

    if milk_type_filter in ('R', 'P'):
        q = q.filter(Sample.milk_type == milk_type_filter)

    samples = q.order_by(Sample.date_registered.desc()).all()

    # Summary stats
    total = len(samples)
    certified = sum(
        1 for s in samples
        if s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)
    )
    in_progress = sum(
        1 for s in samples
        if s.status not in (
            SampleStatus.CERTIFIED, SampleStatus.COMPLETED,
            SampleStatus.REJECTED,
        )
    )
    rejected = sum(1 for s in samples if s.status == SampleStatus.REJECTED)

    fy_start, fy_end = fiscal_year_date_range(year, quarter if quarter else None)
    non_working = fetch_non_working_days(fy_start, fy_end)

    sample_tat = {}
    for s in samples:
        if s.certified_at and s.date_registered and s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED):
            sample_tat[s.id] = calculate_working_days(s.date_registered, s.certified_at, non_working)
        else:
            sample_tat[s.id] = None

    tat_days = [v for v in sample_tat.values() if v is not None]
    avg_tat = round(sum(tat_days) / len(tat_days), 1) if tat_days else None

    # Out-of-spec count
    sample_ids = [s.id for s in samples]
    out_of_spec_count = _out_of_spec_count_for_samples(sample_ids)

    # Resubmission counts per sample
    sample_resubmissions = _resubmission_counts_for_samples(sample_ids)

    # Available years (fiscal)
    available_years = _available_fiscal_years()

    # Pagination
    page = request.args.get('page', 1, type=int)
    total_pages = max(1, (total + REPORT_PER_PAGE - 1) // REPORT_PER_PAGE)
    page = max(1, min(page, total_pages))
    page_start = (page - 1) * REPORT_PER_PAGE
    page_samples = samples[page_start:page_start + REPORT_PER_PAGE]

    return render_template(
        'milk_report.html',
        samples=page_samples,
        year=year,
        quarter=quarter,
        month=month,
        status_filter=status_filter,
        parish_filter=parish_filter,
        milk_type_filter=milk_type_filter,
        available_years=available_years,
        total=total,
        certified=certified,
        in_progress=in_progress,
        rejected=rejected,
        avg_tat=avg_tat,
        out_of_spec_count=out_of_spec_count,
        sample_tat=sample_tat,
        sample_resubmissions=sample_resubmissions,
        SampleStatus=SampleStatus,
        page=page,
        total_pages=total_pages,
    )


@main_bp.route('/reports/milk/download')
@login_required
def milk_report_download():
    """Download milk report as CSV."""
    # Explicit KPI_VIEW permission overrides role restrictions
    # (explicit user grants are evaluated before role-based access).
    if not (current_user.has_permission(Permission.KPI_VIEW)
            or current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                         Role.DEPUTY, Role.ADMIN)):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)
    month = request.args.get('month', type=int, default=0)
    status_filter = request.args.get('status', '')
    parish_filter = request.args.get('parish', '').strip()
    milk_type_filter = request.args.get('milk_type', '').strip()

    q = Sample.query.filter(
        Sample.sample_type == Branch.FOOD_MILK,
    )
    q = _apply_certified_quarter_filter(q, year, quarter, month)

    if status_filter:
        try:
            q = q.filter(Sample.status == SampleStatus(status_filter))
        except ValueError:
            pass

    if parish_filter:
        q = q.filter(Sample.parish.ilike(f'%{parish_filter}%'))

    if milk_type_filter in ('R', 'P'):
        q = q.filter(Sample.milk_type == milk_type_filter)

    samples = q.order_by(Sample.date_registered.desc()).all()

    fy_start, fy_end = fiscal_year_date_range(year, quarter if quarter in (1, 2, 3, 4) else None)
    non_working = fetch_non_working_days(fy_start, fy_end)
    sample_ids = [s.id for s in samples]
    resubmissions = _resubmission_counts_for_samples(sample_ids)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        'Lab Number', 'Source', 'Milk Type', 'Volume', 'Parish',
        'Status', 'Date Received', 'Date Registered',
        'Certified Date', 'Turnaround (days)', 'Report Resubmissions', 'COA Version',
    ])
    for s in samples:
        tat = ''
        if (s.certified_at and s.date_registered
                and s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)):
            tat = calculate_working_days(s.date_registered, s.certified_at, non_working)
        milk_type_label = ''
        if s.milk_type == 'R':
            milk_type_label = 'Raw Milk'
        elif s.milk_type == 'P':
            milk_type_label = 'Processed Milk'
        writer.writerow([
            s.lab_number,
            s.sample_name,
            milk_type_label,
            s.volume or '',
            s.parish or '',
            s.status.value if s.status else '',
            s.date_received.isoformat() if s.date_received else '',
            s.date_registered.strftime('%Y-%m-%d') if s.date_registered else '',
            s.certified_at.strftime('%Y-%m-%d') if s.certified_at else '',
            tat,
            resubmissions.get(s.id, 0),
            s.coa_version if s.coa_version else 1,
        ])

    q_label = f'_Q{quarter}' if quarter in (1, 2, 3, 4) else (f'_M{month}' if month else '')
    filename = f'Milk_Report_{year}{q_label}.csv'
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Toxicology Report
# ---------------------------------------------------------------------------

@main_bp.route('/reports/toxicology')
@login_required
def toxicology_report():
    """Toxicology sample report with filtering."""
    # Explicit KPI_VIEW permission overrides role restrictions
    # (explicit user grants are evaluated before role-based access).
    if not (current_user.has_permission(Permission.KPI_VIEW)
            or current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                         Role.DEPUTY, Role.ADMIN)):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)
    month = request.args.get('month', type=int, default=0)
    status_filter = request.args.get('status', '')
    hospital_filter = request.args.get('hospital', '').strip()
    sample_type_filter = request.args.get('sample_type', '').strip()
    patient_name_filter = request.args.get('patient_name', '').strip()

    q = Sample.query.filter(
        Sample.sample_type == Branch.TOXICOLOGY,
    )
    q = _apply_certified_quarter_filter(q, year, quarter, month)

    if status_filter:
        try:
            st = SampleStatus(status_filter)
            q = q.filter(Sample.status == st)
        except ValueError:
            pass

    if hospital_filter:
        q = q.filter(Sample.source.ilike(f'%{hospital_filter}%'))

    if sample_type_filter:
        q = q.filter(
            Sample.toxicology_sample_type_name.ilike(f'%{sample_type_filter}%')
        )

    if patient_name_filter:
        q = q.filter(Sample.patient_name.ilike(f'%{patient_name_filter}%'))

    samples = q.order_by(Sample.date_registered.desc()).all()

    total = len(samples)
    certified = sum(
        1 for s in samples
        if s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)
    )
    in_progress = sum(
        1 for s in samples
        if s.status not in (
            SampleStatus.CERTIFIED, SampleStatus.COMPLETED,
            SampleStatus.REJECTED,
        )
    )
    rejected = sum(1 for s in samples if s.status == SampleStatus.REJECTED)

    fy_start, fy_end = fiscal_year_date_range(year, quarter if quarter else None)
    non_working = fetch_non_working_days(fy_start, fy_end)

    sample_tat = {}
    for s in samples:
        if s.certified_at and s.date_registered and s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED):
            sample_tat[s.id] = calculate_working_days(s.date_registered, s.certified_at, non_working)
        else:
            sample_tat[s.id] = None

    tat_days = [v for v in sample_tat.values() if v is not None]
    avg_tat = round(sum(tat_days) / len(tat_days), 1) if tat_days else None

    # Out-of-spec count
    sample_ids = [s.id for s in samples]
    out_of_spec_count = _out_of_spec_count_for_samples(sample_ids)

    # Resubmission counts per sample
    sample_resubmissions = _resubmission_counts_for_samples(sample_ids)

    available_years = _available_fiscal_years()

    # Pagination
    page = request.args.get('page', 1, type=int)
    total_pages = max(1, (total + REPORT_PER_PAGE - 1) // REPORT_PER_PAGE)
    page = max(1, min(page, total_pages))
    page_start = (page - 1) * REPORT_PER_PAGE
    page_samples = samples[page_start:page_start + REPORT_PER_PAGE]

    return render_template(
        'toxicology_report.html',
        samples=page_samples,
        year=year,
        quarter=quarter,
        month=month,
        status_filter=status_filter,
        hospital_filter=hospital_filter,
        sample_type_filter=sample_type_filter,
        patient_name_filter=patient_name_filter,
        available_years=available_years,
        total=total,
        certified=certified,
        in_progress=in_progress,
        rejected=rejected,
        avg_tat=avg_tat,
        out_of_spec_count=out_of_spec_count,
        sample_tat=sample_tat,
        sample_resubmissions=sample_resubmissions,
        SampleStatus=SampleStatus,
        page=page,
        total_pages=total_pages,
    )


@main_bp.route('/reports/toxicology/download')
@login_required
def toxicology_report_download():
    """Download toxicology report as CSV."""
    # Explicit KPI_VIEW permission overrides role restrictions
    # (explicit user grants are evaluated before role-based access).
    if not (current_user.has_permission(Permission.KPI_VIEW)
            or current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                         Role.DEPUTY, Role.ADMIN)):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)
    month = request.args.get('month', type=int, default=0)
    status_filter = request.args.get('status', '')
    hospital_filter = request.args.get('hospital', '').strip()
    sample_type_filter = request.args.get('sample_type', '').strip()
    patient_name_filter = request.args.get('patient_name', '').strip()

    q = Sample.query.filter(
        Sample.sample_type == Branch.TOXICOLOGY,
    )
    q = _apply_certified_quarter_filter(q, year, quarter, month)

    if status_filter:
        try:
            q = q.filter(Sample.status == SampleStatus(status_filter))
        except ValueError:
            pass

    if hospital_filter:
        q = q.filter(Sample.source.ilike(f'%{hospital_filter}%'))

    if sample_type_filter:
        q = q.filter(
            Sample.toxicology_sample_type_name.ilike(f'%{sample_type_filter}%')
        )

    if patient_name_filter:
        q = q.filter(Sample.patient_name.ilike(f'%{patient_name_filter}%'))

    samples = q.order_by(Sample.date_registered.desc()).all()

    fy_start, fy_end = fiscal_year_date_range(year, quarter if quarter in (1, 2, 3, 4) else None)
    non_working = fetch_non_working_days(fy_start, fy_end)
    sample_ids = [s.id for s in samples]
    resubmissions = _resubmission_counts_for_samples(sample_ids)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        'Lab Number', 'Sample Name', 'Sample Type', 'Patient Name',
        'Hospital', 'Parish',
        'Status', 'Date Received', 'Date Registered',
        'Certified Date', 'Turnaround (working days)', 'Report Resubmissions',
    ])
    for s in samples:
        tat = ''
        if (s.certified_at and s.date_registered
                and s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)):
            tat = calculate_working_days(s.date_registered, s.certified_at, non_working) or ''
        writer.writerow([
            s.lab_number,
            s.sample_name,
            s.toxicology_sample_type_name or '',
            s.patient_name or '',
            s.ward_clinic or '',
            s.parish or '',
            s.status.value if s.status else '',
            s.date_received.isoformat() if s.date_received else '',
            s.date_registered.strftime('%Y-%m-%d') if s.date_registered else '',
            s.certified_at.strftime('%Y-%m-%d') if s.certified_at else '',
            tat,
            resubmissions.get(s.id, 0),
        ])

    q_label = f'_Q{quarter}' if quarter in (1, 2, 3, 4) else (f'_M{month}' if month else '')
    filename = f'Toxicology_Report_{year}{q_label}.csv'
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Alcohol Report
# ---------------------------------------------------------------------------

@main_bp.route('/reports/alcohol')
@login_required
def alcohol_report():
    """Alcohol sample report with filtering and download."""
    # Explicit KPI_VIEW permission overrides role restrictions
    # (explicit user grants are evaluated before role-based access).
    if not (current_user.has_permission(Permission.KPI_VIEW)
            or current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                         Role.DEPUTY, Role.ADMIN)):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)
    month = request.args.get('month', type=int, default=0)
    status_filter = request.args.get('status', '')
    sample_name_filter = request.args.get('sample_name', '').strip()
    alcohol_type_filter = request.args.get('alcohol_type', '').strip()

    q = Sample.query.filter(
        Sample.sample_type == Branch.FOOD_ALCOHOL,
    )
    q = _apply_certified_quarter_filter(q, year, quarter, month)

    if status_filter:
        try:
            st = SampleStatus(status_filter)
            q = q.filter(Sample.status == st)
        except ValueError:
            pass

    if sample_name_filter:
        q = q.filter(Sample.sample_name.ilike(f'%{sample_name_filter}%'))

    if alcohol_type_filter:
        q = q.filter(Sample.alcohol_type.ilike(f'%{alcohol_type_filter}%'))

    samples = q.order_by(Sample.date_registered.desc()).all()

    total = len(samples)
    certified = sum(
        1 for s in samples
        if s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)
    )
    in_progress = sum(
        1 for s in samples
        if s.status not in (
            SampleStatus.CERTIFIED, SampleStatus.COMPLETED,
            SampleStatus.REJECTED,
        )
    )
    rejected = sum(1 for s in samples if s.status == SampleStatus.REJECTED)

    fy_start, fy_end = fiscal_year_date_range(year, quarter if quarter else None)
    non_working = fetch_non_working_days(fy_start, fy_end)

    sample_tat = {}
    for s in samples:
        if s.certified_at and s.date_registered and s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED):
            sample_tat[s.id] = calculate_working_days(s.date_registered, s.certified_at, non_working)
        else:
            sample_tat[s.id] = None

    tat_days = [v for v in sample_tat.values() if v is not None]
    avg_tat = round(sum(tat_days) / len(tat_days), 1) if tat_days else None

    # Out-of-spec count
    sample_ids = [s.id for s in samples]
    out_of_spec_count = _out_of_spec_count_for_samples(sample_ids)

    # Resubmission counts per sample
    sample_resubmissions = _resubmission_counts_for_samples(sample_ids)

    # Avg TAT breakdown by alcohol type
    alcohol_type_tat = {}
    alcohol_type_labels = [
        'Alcohol Determination',
        'Denatured Alcohol (bitrex)',
        'Alcohol Determination and Denatured',
    ]
    for alc_type in alcohol_type_labels:
        type_days = [
            calculate_working_days(s.date_registered, s.certified_at, non_working)
            for s in samples
            if s.alcohol_type == alc_type
            and s.certified_at and s.date_registered
            and s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)
        ]
        type_days = [d for d in type_days if d is not None]
        alcohol_type_tat[alc_type] = (
            round(sum(type_days) / len(type_days), 1) if type_days else None
        )

    available_years = _available_fiscal_years()

    # Pagination
    page = request.args.get('page', 1, type=int)
    total_pages = max(1, (total + REPORT_PER_PAGE - 1) // REPORT_PER_PAGE)
    page = max(1, min(page, total_pages))
    page_start = (page - 1) * REPORT_PER_PAGE
    page_samples = samples[page_start:page_start + REPORT_PER_PAGE]

    return render_template(
        'alcohol_report.html',
        samples=page_samples,
        year=year,
        quarter=quarter,
        month=month,
        status_filter=status_filter,
        sample_name_filter=sample_name_filter,
        alcohol_type_filter=alcohol_type_filter,
        available_years=available_years,
        total=total,
        certified=certified,
        in_progress=in_progress,
        rejected=rejected,
        avg_tat=avg_tat,
        out_of_spec_count=out_of_spec_count,
        sample_tat=sample_tat,
        sample_resubmissions=sample_resubmissions,
        alcohol_type_tat=alcohol_type_tat,
        SampleStatus=SampleStatus,
        page=page,
        total_pages=total_pages,
    )


@main_bp.route('/reports/alcohol/download')
@login_required
def alcohol_report_download():
    """Download alcohol report as CSV."""
    # Explicit KPI_VIEW permission overrides role restrictions
    # (explicit user grants are evaluated before role-based access).
    if not (current_user.has_permission(Permission.KPI_VIEW)
            or current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                         Role.DEPUTY, Role.ADMIN)):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)
    month = request.args.get('month', type=int, default=0)
    status_filter = request.args.get('status', '')
    sample_name_filter = request.args.get('sample_name', '').strip()
    alcohol_type_filter = request.args.get('alcohol_type', '').strip()

    q = Sample.query.filter(
        Sample.sample_type == Branch.FOOD_ALCOHOL,
    )
    q = _apply_certified_quarter_filter(q, year, quarter, month)

    if status_filter:
        try:
            q = q.filter(Sample.status == SampleStatus(status_filter))
        except ValueError:
            pass

    if sample_name_filter:
        q = q.filter(Sample.sample_name.ilike(f'%{sample_name_filter}%'))

    if alcohol_type_filter:
        q = q.filter(Sample.alcohol_type.ilike(f'%{alcohol_type_filter}%'))

    samples = q.order_by(Sample.date_registered.desc()).all()

    fy_start, fy_end = fiscal_year_date_range(year, quarter if quarter in (1, 2, 3, 4) else None)
    non_working = fetch_non_working_days(fy_start, fy_end)
    sample_ids = [s.id for s in samples]
    resubmissions = _resubmission_counts_for_samples(sample_ids)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        'Lab Number', 'Sample Name', 'Alcohol Type', 'Claim/Butt #',
        'Batch/Lot #', 'Status', 'Date Received', 'Date Registered',
        'Certified Date', 'Turnaround (working days)', 'Report Resubmissions', 'COA Version',
    ])
    for s in samples:
        tat = ''
        if (s.certified_at and s.date_registered
                and s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)):
            tat = calculate_working_days(s.date_registered, s.certified_at, non_working) or ''
        writer.writerow([
            s.lab_number,
            s.sample_name,
            s.alcohol_type or '',
            s.claim_butt_number or '',
            s.batch_lot_number or '',
            s.status.value if s.status else '',
            s.date_received.isoformat() if s.date_received else '',
            s.date_registered.strftime('%Y-%m-%d') if s.date_registered else '',
            s.certified_at.strftime('%Y-%m-%d') if s.certified_at else '',
            tat,
            resubmissions.get(s.id, 0),
            s.coa_version if s.coa_version else 1,
        ])

    q_label = f'_Q{quarter}' if quarter in (1, 2, 3, 4) else (f'_M{month}' if month else '')
    filename = f'Alcohol_Report_{year}{q_label}.csv'
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Toxicology KPI Report
# ---------------------------------------------------------------------------

@main_bp.route('/kpi/toxicology')
@login_required
def kpi_toxicology():
    """Toxicology-specific KPI report."""
    # Explicit KPI_VIEW permission overrides role restrictions
    # (explicit user grants are evaluated before role-based access).
    if not (current_user.has_permission(Permission.KPI_VIEW)
            or current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                         Role.DEPUTY, Role.ADMIN)):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int, default=_current_fiscal_year())
    available_years = _available_fiscal_years()

    fiscal_q_labels = {1: 'Q1 (Apr-Jun)', 2: 'Q2 (Jul-Sep)',
                       3: 'Q3 (Oct-Dec)', 4: 'Q4 (Jan-Mar)'}
    quarters_data = []
    for q_num in range(1, 5):
        start, end = fiscal_year_date_range(year, q_num)

        base_q = Sample.query.filter(
            Sample.sample_type == Branch.TOXICOLOGY,
            Sample.date_registered >= start,
            Sample.date_registered <= end,
        )
        total = base_q.count()
        certified = base_q.filter(
            Sample.status.in_([SampleStatus.CERTIFIED, SampleStatus.COMPLETED])
        ).count()
        in_progress = base_q.filter(
            Sample.status.notin_([
                SampleStatus.CERTIFIED, SampleStatus.COMPLETED,
                SampleStatus.REJECTED
            ])
        ).count()
        rejected = base_q.filter(
            Sample.status == SampleStatus.REJECTED
        ).count()

        cert_samples = base_q.filter(
            Sample.status.in_([SampleStatus.CERTIFIED, SampleStatus.COMPLETED]),
            Sample.certified_at.isnot(None),
        ).all()
        if cert_samples:
            non_working = _prefetch_tat_non_working_days(cert_samples)
            days_list = [
                calculate_working_days(s.date_registered, s.certified_at, non_working)
                for s in cert_samples
                if s.certified_at and s.date_registered
            ]
            days_list = [d for d in days_list if d is not None]
            avg_tat = round(sum(days_list) / len(days_list), 1) if days_list else None
        else:
            avg_tat = None

        quarters_data.append({
            'quarter': q_num,
            'label': fiscal_q_labels[q_num],
            'total': total,
            'certified': certified,
            'in_progress': in_progress,
            'rejected': rejected,
            'avg_tat': avg_tat,
        })

    return render_template(
        'kpi_toxicology.html',
        quarters_data=quarters_data,
        year=year,
        available_years=available_years,
    )


# ---------------------------------------------------------------------------
# All Branches Combined Report
# ---------------------------------------------------------------------------

def _can_view_all_branches_report():
    """Return True if the current user may view the all-branches report."""
    return (
        current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                  Role.DEPUTY, Role.ADMIN)
        or current_user.has_permission(Permission.VIEW_ALL_BRANCHES_REPORT)
    )


@main_bp.route('/reports/all-branches')
@login_required
def all_branches_report():
    """Combined report showing samples from all branches with carry-forward logic."""
    if not _can_view_all_branches_report():
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int, default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)  # 0 = all
    month = request.args.get('month', type=int, default=0)
    status_filter = request.args.get('status', '')
    branch_filter = request.args.get('branch', '')

    q = Sample.query
    # Carry forward uncertified samples from previous quarters
    q = _apply_certified_quarter_filter(q, year, quarter, month)

    if branch_filter:
        try:
            br = Branch[branch_filter]
            q = q.filter(Sample.sample_type == br)
        except KeyError:
            pass

    if status_filter:
        try:
            st = SampleStatus(status_filter)
            q = q.filter(Sample.status == st)
        except ValueError:
            pass

    samples = q.order_by(Sample.date_registered.desc()).all()

    total = len(samples)
    certified = sum(
        1 for s in samples
        if s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)
    )
    in_progress = sum(
        1 for s in samples
        if s.status not in (
            SampleStatus.CERTIFIED, SampleStatus.COMPLETED,
            SampleStatus.REJECTED,
        )
    )
    rejected = sum(1 for s in samples if s.status == SampleStatus.REJECTED)

    fy_start, fy_end = fiscal_year_date_range(year, quarter if quarter else None)
    non_working = fetch_non_working_days(fy_start, fy_end)

    sample_tat = {}
    for s in samples:
        if s.certified_at and s.date_registered and s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED):
            sample_tat[s.id] = calculate_working_days(s.date_registered, s.certified_at, non_working)
        else:
            sample_tat[s.id] = None

    tat_days = [v for v in sample_tat.values() if v is not None]
    avg_tat = round(sum(tat_days) / len(tat_days), 1) if tat_days else None

    sample_ids = [s.id for s in samples]
    out_of_spec_count = _out_of_spec_count_for_samples(sample_ids)
    sample_resubmissions = _resubmission_counts_for_samples(sample_ids)

    # Per-branch breakdown counts
    branch_counts = {}
    for br in Branch:
        branch_counts[br] = sum(1 for s in samples if s.sample_type == br)

    available_years = _available_fiscal_years()

    page = request.args.get('page', 1, type=int)
    total_pages = max(1, (total + REPORT_PER_PAGE - 1) // REPORT_PER_PAGE)
    page = max(1, min(page, total_pages))
    page_start = (page - 1) * REPORT_PER_PAGE
    page_samples = samples[page_start:page_start + REPORT_PER_PAGE]

    return render_template(
        'all_branches_report.html',
        samples=page_samples,
        year=year,
        quarter=quarter,
        month=month,
        status_filter=status_filter,
        branch_filter=branch_filter,
        available_years=available_years,
        total=total,
        certified=certified,
        in_progress=in_progress,
        rejected=rejected,
        avg_tat=avg_tat,
        out_of_spec_count=out_of_spec_count,
        sample_tat=sample_tat,
        sample_resubmissions=sample_resubmissions,
        branch_counts=branch_counts,
        Branch=Branch,
        SampleStatus=SampleStatus,
        page=page,
        total_pages=total_pages,
    )


@main_bp.route('/reports/all-branches/download')
@login_required
def all_branches_report_download():
    """Download all-branches report as CSV."""
    if not _can_view_all_branches_report():
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int, default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)
    month = request.args.get('month', type=int, default=0)
    status_filter = request.args.get('status', '')
    branch_filter = request.args.get('branch', '')

    q = Sample.query
    q = _apply_certified_quarter_filter(q, year, quarter, month)

    if branch_filter:
        try:
            br = Branch[branch_filter]
            q = q.filter(Sample.sample_type == br)
        except KeyError:
            pass

    if status_filter:
        try:
            q = q.filter(Sample.status == SampleStatus(status_filter))
        except ValueError:
            pass

    samples = q.order_by(Sample.date_registered.desc()).all()

    fy_start, fy_end = fiscal_year_date_range(year, quarter if quarter in (1, 2, 3, 4) else None)
    non_working = fetch_non_working_days(fy_start, fy_end)
    sample_ids = [s.id for s in samples]
    resubmissions = _resubmission_counts_for_samples(sample_ids)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        'Lab Number', 'Sample Name', 'Branch', 'Status',
        'Date Received', 'Date Registered', 'Certified Date',
        'Turnaround (working days)', 'Report Resubmissions', 'COA Version',
    ])
    for s in samples:
        tat = ''
        if (s.certified_at and s.date_registered
                and s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)):
            tat = calculate_working_days(s.date_registered, s.certified_at, non_working) or ''
        writer.writerow([
            s.lab_number,
            s.sample_name,
            s.sample_type.value if s.sample_type else '',
            s.status.value if s.status else '',
            s.date_received.isoformat() if s.date_received else '',
            s.date_registered.strftime('%Y-%m-%d') if s.date_registered else '',
            s.certified_at.strftime('%Y-%m-%d') if s.certified_at else '',
            tat,
            resubmissions.get(s.id, 0),
            s.coa_version if s.coa_version else 1,
        ])

    q_label = f'_Q{quarter}' if quarter in (1, 2, 3, 4) else (f'_M{month}' if month else '')
    b_label = f'_{branch_filter}' if branch_filter else ''
    filename = f'AllBranches_Report_{year}{q_label}{b_label}.csv'
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Analyst Performance Report
# ---------------------------------------------------------------------------

def _can_view_analyst_report():
    """Return True if the current user may view the analyst performance report."""
    return (
        current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                  Role.DEPUTY, Role.ADMIN)
        or current_user.has_permission(Permission.VIEW_ANALYST_PERFORMANCE_REPORT)
    )


@main_bp.route('/reports/analysts')
@login_required
def analyst_report():
    """Analyst performance report: tests completed per analyst with filters."""
    if not _can_view_analyst_report():
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)  # 0 = all
    branch_filter = request.args.get('branch', '')
    analyst_id = request.args.get('analyst_id', type=int, default=0)
    search = request.args.get('search', '').strip()

    # Resubmission type filter: read selected types from request.
    # 'resub_type' is a multi-value checkbox list; empty list means use default.
    # The special value 'all' means no filter (count all types).
    valid_type_keys = {k for k, _ in RESUBMISSION_TYPES}
    raw_resub_types = request.args.getlist('resub_type')
    if not raw_resub_types:
        # No filter submitted — fall back to system default
        resub_filter = _get_default_resubmission_types()   # None = all, list = specific
        if resub_filter is None:
            resub_selected = ['all']
        else:
            resub_selected = [t for t in resub_filter if t in valid_type_keys]
            if not resub_selected:
                resub_selected = ['all']
    elif 'all' in raw_resub_types:
        resub_filter = None   # count every type
        resub_selected = ['all']
    else:
        resub_selected = [t for t in raw_resub_types if t in valid_type_keys]
        resub_filter = resub_selected if resub_selected else None

    # Workflow status filter: multi-select by SampleStatus name.
    # 'status' is a multi-value list; empty means show all statuses.
    valid_status_names = {name for name, _ in WORKFLOW_STATUSES}
    raw_status_filter = request.args.getlist('status')
    if not raw_status_filter or 'all' in raw_status_filter:
        status_filter_names = []   # empty = no filter
        status_selected = ['all']
    else:
        status_filter_names = [s for s in raw_status_filter if s in valid_status_names]
        status_selected = status_filter_names if status_filter_names else ['all']

    # Build base query on assignments
    q = SampleAssignment.query.join(
        Sample, SampleAssignment.sample_id == Sample.id
    )
    q = _fiscal_year_filter(q, SampleAssignment.assigned_date, year,
                            quarter if quarter else None)

    if branch_filter:
        try:
            br = Branch[branch_filter]
            q = q.filter(Sample.sample_type == br)
        except KeyError:
            pass

    # Apply workflow status filter on the sample's current status
    if status_filter_names:
        resolved_statuses = []
        for name in status_filter_names:
            try:
                resolved_statuses.append(SampleStatus[name])
            except KeyError:
                pass
        if resolved_statuses:
            q = q.filter(Sample.status.in_(resolved_statuses))

    assignments = q.order_by(SampleAssignment.assigned_date.desc()).all()

    # Group by analyst; track per-status counts for breakdown display
    analyst_data = {}
    for a in assignments:
        cid = a.chemist_id
        if cid not in analyst_data:
            analyst_data[cid] = {
                'id': cid,
                'name': a.chemist.full_name if a.chemist else 'Unknown',
                'total': 0,
                'completed': 0,
                'in_progress': 0,
                'tests': [],
                'status_counts': {},       # {SampleStatus.name: count} — current sample workflow status
                'assign_status_counts': {},  # {AssignmentStatus.name: count} — assignment-level status
                'sample_ids': set(),       # unique sample IDs for this analyst
            }
        entry = analyst_data[cid]
        entry['total'] += 1
        entry['sample_ids'].add(a.sample_id)
        if a.status in (AssignmentStatus.ACCEPTED, AssignmentStatus.COMPLETED):
            entry['completed'] += 1
        elif a.status != AssignmentStatus.REJECTED:
            entry['in_progress'] += 1
        entry['tests'].append(a)
        # Track assignment-level status counts
        if a.status:
            aname = a.status.name
            entry['assign_status_counts'][aname] = entry['assign_status_counts'].get(aname, 0) + 1
        # Track sample-level workflow status counts
        s_status = a.sample.status
        if s_status:
            entry['status_counts'][s_status.name] = entry['status_counts'].get(s_status.name, 0) + 1

    # Sort analysts by completed tests descending
    sort_by = request.args.get('sort', 'completed')
    sort_dir = request.args.get('dir', 'desc')
    reverse = (sort_dir == 'desc')
    if sort_by == 'name':
        analyst_list = sorted(analyst_data.values(), key=lambda x: x['name'].lower(), reverse=reverse)
    elif sort_by == 'total':
        analyst_list = sorted(analyst_data.values(), key=lambda x: x['total'], reverse=reverse)
    else:
        analyst_list = sorted(analyst_data.values(), key=lambda x: x['completed'], reverse=reverse)

    if search:
        analyst_list = [a for a in analyst_list if search.lower() in a['name'].lower()]

    # Pagination for the analyst summary table (Python-level)
    SUMMARY_PER_PAGE = 20
    summary_page = request.args.get('summary_page', 1, type=int)
    total_analyst_count = len(analyst_list)
    summary_start = (summary_page - 1) * SUMMARY_PER_PAGE
    summary_end = summary_start + SUMMARY_PER_PAGE
    analyst_page_items = analyst_list[summary_start:summary_end]
    total_summary_pages = max(1, (total_analyst_count + SUMMARY_PER_PAGE - 1) // SUMMARY_PER_PAGE)
    summary_page = max(1, min(summary_page, total_summary_pages))

    # Available years (fiscal)
    available_years = _available_fiscal_years()

    # Summary totals
    total_assignments = len(assignments)
    total_completed = sum(d['completed'] for d in analyst_data.values())

    # Fetch per-type resubmission breakdown for all assignments in one query,
    # then aggregate per analyst so we can display per-type counts.
    all_assignment_ids = [a.id for a in assignments]
    type_breakdown_by_assignment = _resubmission_type_breakdown_for_assignments(all_assignment_ids)

    for cid, data in analyst_data.items():
        breakdown = {}
        for assign in data['tests']:
            for tk, cnt in type_breakdown_by_assignment.get(assign.id, {}).items():
                breakdown[tk] = breakdown.get(tk, 0) + cnt
        data['resub_breakdown'] = breakdown
        # Filtered total for this analyst (respects the selected type filter)
        if resub_filter is None:
            data['resub_filtered'] = sum(breakdown.values())
        else:
            data['resub_filtered'] = sum(
                cnt for tk, cnt in breakdown.items() if tk in resub_filter
            )
        # Per-analyst preliminary resubmission average and grade (0 / 1 / 2+)
        prelim_cnt = breakdown.get('preliminary', 0)
        unique_sample_cnt = len(data['sample_ids'])
        data['unique_sample_count'] = unique_sample_cnt
        data['resub_avg'] = round(prelim_cnt / unique_sample_cnt, 2) if unique_sample_cnt > 0 else 0.0
        if prelim_cnt == 0:
            data['resub_grade'] = '0'
        elif prelim_cnt == 1:
            data['resub_grade'] = '1'
        else:
            data['resub_grade'] = '2+'

    total_resubmissions = sum(d['resub_filtered'] for d in analyst_data.values())

    # Per-return-type dashboard totals (always computed from full breakdown,
    # independent of the resub_filter so all cards are always populated).
    total_returned_correction = sum(
        d['resub_breakdown'].get('preliminary', 0) for d in analyst_data.values()
    )
    total_returned_deputy = sum(
        d['resub_breakdown'].get('deputy', 0) for d in analyst_data.values()
    )
    total_returned_hod = sum(
        d['resub_breakdown'].get('hod', 0) for d in analyst_data.values()
    )
    total_submitted = sum(
        d['assign_status_counts'].get('REPORT_SUBMITTED', 0) for d in analyst_data.values()
    )
    total_certified = sum(
        d['status_counts'].get('CERTIFIED', 0) for d in analyst_data.values()
    )
    total_completed_samples = sum(
        d['status_counts'].get('COMPLETED', 0) for d in analyst_data.values()
    )

    # Overall average preliminary resubmissions per unique sample across all filtered data
    total_unique_samples = len({a.sample_id for a in assignments})
    avg_resubmissions_per_sample = (
        round(total_returned_correction / total_unique_samples, 2)
        if total_unique_samples > 0 else 0.0
    )

    # Selected analyst detail view with pagination and sort
    selected_analyst = None
    detail_items = []
    detail_page = request.args.get('detail_page', 1, type=int)
    detail_sort = request.args.get('detail_sort', 'assigned')
    detail_dir = request.args.get('detail_dir', 'desc')
    detail_total_pages = 1
    DETAIL_PER_PAGE = 25

    if analyst_id and analyst_id in analyst_data:
        selected_analyst = analyst_data[analyst_id]
        tests = list(selected_analyst['tests'])

        # Sort the detail tests; nulls always placed at the end of the result
        detail_reverse = (detail_dir == 'desc')
        null_date = date.min if detail_reverse else date.max
        if detail_sort == 'lab':
            tests.sort(key=lambda t: t.sample.lab_number or '', reverse=detail_reverse)
        elif detail_sort == 'sample':
            tests.sort(key=lambda t: t.sample.sample_name or '', reverse=detail_reverse)
        elif detail_sort == 'lab_type':
            tests.sort(key=lambda t: t.sample.sample_type.value if t.sample.sample_type else '', reverse=detail_reverse)
        elif detail_sort == 'test':
            tests.sort(key=lambda t: t.test_name or '', reverse=detail_reverse)
        elif detail_sort == 'status':
            tests.sort(key=lambda t: t.status.value if t.status else '', reverse=detail_reverse)
        elif detail_sort == 'completed_date':
            tests.sort(key=lambda t: t.date_completed or null_date, reverse=detail_reverse)
        else:  # 'assigned' (default)
            tests.sort(key=lambda t: t.assigned_date or null_date, reverse=detail_reverse)

        total_detail = len(tests)
        detail_total_pages = max(1, (total_detail + DETAIL_PER_PAGE - 1) // DETAIL_PER_PAGE)
        detail_page = max(1, min(detail_page, detail_total_pages))
        d_start = (detail_page - 1) * DETAIL_PER_PAGE
        detail_items = tests[d_start:d_start + DETAIL_PER_PAGE]

    # Per-assignment type breakdown and filtered totals for the detail table
    detail_assignment_ids = [a.id for a in detail_items]
    assignment_type_breakdowns = {
        aid: type_breakdown_by_assignment.get(aid, {})
        for aid in detail_assignment_ids
    }
    assignment_resubmissions = {
        aid: (sum(bdown.values()) if resub_filter is None
              else sum(cnt for tk, cnt in bdown.items() if tk in resub_filter))
        for aid, bdown in assignment_type_breakdowns.items()
    }

    # Build human-readable labels for selected resubmission types (transparency)
    type_label_map = dict(RESUBMISSION_TYPES)
    if resub_filter is None:
        included_type_labels = ['All Review Types']
    else:
        included_type_labels = [type_label_map.get(t, t.title()) for t in resub_filter]

    return render_template(
        'analyst_report.html',
        analyst_list=analyst_page_items,
        analyst_list_all=analyst_list,
        year=year,
        quarter=quarter,
        branch_filter=branch_filter,
        search=search,
        available_years=available_years,
        total_assignments=total_assignments,
        total_completed=total_completed,
        total_analysts=total_analyst_count,
        total_resubmissions=total_resubmissions,
        total_submitted=total_submitted,
        total_returned_correction=total_returned_correction,
        total_returned_deputy=total_returned_deputy,
        total_returned_hod=total_returned_hod,
        total_certified=total_certified,
        total_completed_samples=total_completed_samples,
        total_unique_samples=total_unique_samples,
        avg_resubmissions_per_sample=avg_resubmissions_per_sample,
        Branch=Branch,
        sort_by=sort_by,
        sort_dir=sort_dir,
        AssignmentStatus=AssignmentStatus,
        # Summary pagination
        summary_page=summary_page,
        total_summary_pages=total_summary_pages,
        # Analyst detail
        analyst_id=analyst_id,
        selected_analyst=selected_analyst,
        detail_items=detail_items,
        detail_page=detail_page,
        detail_total_pages=detail_total_pages,
        detail_sort=detail_sort,
        detail_dir=detail_dir,
        assignment_resubmissions=assignment_resubmissions,
        assignment_type_breakdowns=assignment_type_breakdowns,
        # Resubmission type filter
        resubmission_types=RESUBMISSION_TYPES,
        resub_selected=resub_selected,
        included_type_labels=included_type_labels,
        type_label_map=type_label_map,
        # Workflow status filter
        workflow_statuses=WORKFLOW_STATUSES,
        status_selected=status_selected,
        SampleStatus=SampleStatus,
    )


@main_bp.route('/reports/analysts/download')
@login_required
def analyst_report_download():
    """Download analyst performance report as CSV."""
    if not _can_view_analyst_report():
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)
    branch_filter = request.args.get('branch', '')

    # Resubmission type filter (mirrors analyst_report route logic)
    valid_type_keys = {k for k, _ in RESUBMISSION_TYPES}
    raw_resub_types = request.args.getlist('resub_type')
    if not raw_resub_types:
        resub_filter = _get_default_resubmission_types()
        if resub_filter is None:
            resub_selected = ['all']
        else:
            resub_selected = [t for t in resub_filter if t in valid_type_keys]
            if not resub_selected:
                resub_selected = ['all']
    elif 'all' in raw_resub_types:
        resub_filter = None
        resub_selected = ['all']
    else:
        resub_selected = [t for t in raw_resub_types if t in valid_type_keys]
        resub_filter = resub_selected if resub_selected else None

    # Workflow status filter (mirrors analyst_report route logic)
    valid_status_names = {name for name, _ in WORKFLOW_STATUSES}
    raw_status_filter = request.args.getlist('status')
    if not raw_status_filter or 'all' in raw_status_filter:
        status_filter_names = []
    else:
        status_filter_names = [s for s in raw_status_filter if s in valid_status_names]

    type_label_map = dict(RESUBMISSION_TYPES)
    if resub_filter is None:
        included_type_labels = 'All Review Types'
    else:
        included_type_labels = ', '.join(type_label_map.get(t, t.title()) for t in resub_filter)

    status_label_map = dict(WORKFLOW_STATUSES)
    if not status_filter_names:
        included_status_labels = 'All Statuses'
    else:
        included_status_labels = ', '.join(status_label_map.get(s, s) for s in status_filter_names)

    q = SampleAssignment.query.join(
        Sample, SampleAssignment.sample_id == Sample.id
    ).join(
        User, SampleAssignment.chemist_id == User.id
    )
    q = _fiscal_year_filter(q, SampleAssignment.assigned_date, year,
                            quarter if quarter in (1, 2, 3, 4) else None)

    if branch_filter:
        try:
            br = Branch[branch_filter]
            q = q.filter(Sample.sample_type == br)
        except KeyError:
            pass

    # Apply workflow status filter
    if status_filter_names:
        resolved_statuses = []
        for name in status_filter_names:
            try:
                resolved_statuses.append(SampleStatus[name])
            except KeyError:
                pass
        if resolved_statuses:
            q = q.filter(Sample.status.in_(resolved_statuses))

    assignments = q.order_by(User.last_name, SampleAssignment.assigned_date.desc()).all()

    assignment_ids = [a.id for a in assignments]
    type_breakdown = _resubmission_type_breakdown_for_assignments(assignment_ids)

    # Compute summary metrics for the CSV header block
    total_prelim_returns = sum(
        type_breakdown.get(aid, {}).get('preliminary', 0) for aid in assignment_ids
    )
    total_unique_samples_csv = len({a.sample_id for a in assignments})
    avg_resubs_csv = (
        round(total_prelim_returns / total_unique_samples_csv, 2)
        if total_unique_samples_csv > 0 else 0.0
    )

    # Return-stage column headers — always included for full transparency
    # These match the RESUBMISSION_TYPES keys: preliminary, technical, deputy, hod, unspecified
    return_col_headers = [label for _, label in RESUBMISSION_TYPES]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([f'Workflow Status Filter: {included_status_labels}'])
    writer.writerow([f'Total Resubmissions (Returned for Correction from Preliminary Review): {total_prelim_returns}'])
    writer.writerow([f'Unique Samples: {total_unique_samples_csv}'])
    writer.writerow([f'Average Resubmissions per Sample: {avg_resubs_csv}'])
    writer.writerow([])
    writer.writerow([
        'Analyst', 'Lab Number', 'Sample Name', 'Laboratory',
        'Test Name', 'Assignment Status', 'Sample Workflow Status',
        'Assigned Date', 'Date Completed',
        # Return counts broken out by the stage from which the report was returned
        'Returned for Correction (Preliminary)',
        'Returned from Senior Chemist Review',
        'Returned by Deputy',
        'Returned by HOD',
        'Unspecified Returns',
        'Total Returns',
    ])
    for a in assignments:
        bdown = type_breakdown.get(a.id, {})
        prelim_cnt  = bdown.get('preliminary', 0)
        tech_cnt    = bdown.get('technical', 0)
        deputy_cnt  = bdown.get('deputy', 0)
        hod_cnt     = bdown.get('hod', 0)
        unspec_cnt  = bdown.get('unspecified', 0)
        total_cnt   = prelim_cnt + tech_cnt + deputy_cnt + hod_cnt + unspec_cnt
        writer.writerow([
            a.chemist.full_name if a.chemist else 'Unknown',
            a.sample.lab_number,
            a.sample.sample_name,
            a.sample.sample_type.value if a.sample.sample_type else '',
            a.test_name,
            a.status.value if a.status else '',
            a.sample.status.value if a.sample.status else '',
            a.assigned_date.strftime('%Y-%m-%d') if a.assigned_date else '',
            a.date_completed.strftime('%Y-%m-%d') if a.date_completed else '',
            prelim_cnt,
            tech_cnt,
            deputy_cnt,
            hod_cnt,
            unspec_cnt,
            total_cnt,
        ])

    q_label = f'_Q{quarter}' if quarter in (1, 2, 3, 4) else ''
    b_label = f'_{branch_filter}' if branch_filter else ''
    filename = f'Analyst_Report_{year}{q_label}{b_label}.csv'
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# QA Performance Summary Report
# ---------------------------------------------------------------------------

# Branch categories used by the QA Performance Summary report
_QA_FOOD_BRANCHES   = {Branch.FOOD_MILK, Branch.FOOD_ALCOHOL}
_QA_TOX_BRANCHES    = {Branch.TOXICOLOGY}
_QA_PHARM_BRANCHES  = {Branch.PHARMACEUTICAL, Branch.PHARMACEUTICAL_NR}


def _qa_branch_category(branch):
    """Map a Branch enum value to one of 'food', 'tox', 'pharm', or None."""
    if branch in _QA_FOOD_BRANCHES:
        return 'food'
    if branch in _QA_TOX_BRANCHES:
        return 'tox'
    if branch in _QA_PHARM_BRANCHES:
        return 'pharm'
    return None


def _can_view_qa_performance():
    """Return True if the current user may view the QA Performance Summary."""
    return (
        current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                  Role.DEPUTY, Role.ADMIN)
        or current_user.has_permission(Permission.VIEW_ANALYST_PERFORMANCE_REPORT)
    )


def _qa_format_dt(value):
    """Format a datetime/date value for report audit fields."""
    if not value:
        return ''
    return value.strftime('%Y-%m-%d %H:%M') if hasattr(value, 'strftime') else str(value)


def _qa_unique_csv(values):
    """Return a stable comma-separated string for a collection of display values."""
    cleaned = sorted({v for v in values if v})
    return ', '.join(cleaned) if cleaned else 'not determinable from available records'


def _qa_corrected_sample_report(assignments):
    """Build corrected sample-level QA return/resubmission counts and audit rows.

    Preliminary Review returns are sourced only from ReviewHistory rows with
    review_type='preliminary' and action='returned'.  Other resubmissions are
    sourced from report DocumentVersion rows with upload_label='resubmission',
    excluding preliminary resubmission uploads from the combined total to avoid
    double-counting the same Preliminary Review return event.
    """
    if not assignments:
        return {
            'sample_rows': [],
            'analyst_breakdown': [],
            'quality_issues': [],
            'exclusions': [],
            'totals': {
                'samples': 0,
                'samples_with_prelim_returns': 0,
                'preliminary_returns': 0,
                'other_resubmissions': 0,
                'combined_total': 0,
            },
        }

    type_labels = dict(RESUBMISSION_TYPES)
    type_keys = [key for key, _ in RESUBMISSION_TYPES]
    assignment_by_id = {a.id: a for a in assignments}
    sample_by_id = {}
    analysts_by_sample = {}
    for a in assignments:
        if not a.sample:
            continue
        sample_by_id[a.sample_id] = a.sample
        analysts_by_sample.setdefault(a.sample_id, set()).add(
            a.chemist.full_name if a.chemist else 'Unknown'
        )

    sample_ids = sorted(sample_by_id.keys())
    rows = {}
    for sid in sample_ids:
        sample = sample_by_id[sid]
        rows[sid] = {
            'sample_id': sid,
            'lab_number': sample.lab_number,
            'sample_name': sample.sample_name,
            'lab_type': sample.sample_type.value if sample.sample_type else '',
            'analysts': set(analysts_by_sample.get(sid, set())),
            'preliminary_returns': 0,
            'other_resubmissions': 0,
            'combined_total': 0,
            'type_breakdown': {key: 0 for key in type_keys},
            'preliminary_return_ids': [],
            'other_resubmission_ids': [],
            'audit_events': [],
            'quality_flags': [],
        }

    analyst_data = {}

    def analyst_entry(name):
        key = name or 'not determinable from available records'
        if key not in analyst_data:
            analyst_data[key] = {
                'analyst': key,
                'samples': set(),
                'preliminary_returns': 0,
                'other_resubmissions': 0,
                'combined_total': 0,
                'event_ids': [],
            }
        return analyst_data[key]

    quality_issues = []
    exclusions = []

    def add_issue(sid, issue_type, source, event_id, detail):
        sample = sample_by_id.get(sid)
        lab = sample.lab_number if sample else str(sid)
        issue = {
            'sample_id': sid,
            'lab_number': lab,
            'issue_type': issue_type,
            'source': source,
            'event_id': event_id,
            'detail': detail,
        }
        quality_issues.append(issue)
        if sid in rows:
            rows[sid]['quality_flags'].append(f'{issue_type}: {detail}')

    prelim_events = (
        ReviewHistory.query
        .filter(
            ReviewHistory.sample_id.in_(sample_ids),
            ReviewHistory.review_type == 'preliminary',
            ReviewHistory.action == 'returned',
        )
        .order_by(ReviewHistory.reviewed_at.asc(), ReviewHistory.id.asc())
        .all()
    )
    resubmissions = (
        DocumentVersion.query
        .filter(
            DocumentVersion.sample_id.in_(sample_ids),
            DocumentVersion.document_type == 'report',
            DocumentVersion.upload_label == 'resubmission',
        )
        .order_by(DocumentVersion.sample_id.asc(), DocumentVersion.id.asc())
        .all()
    )
    extra_assignment_ids = {
        event.assignment_id for event in prelim_events
        if event.assignment_id and event.assignment_id not in assignment_by_id
    }
    extra_assignment_ids.update(
        doc.assignment_id for doc in resubmissions
        if doc.assignment_id and doc.assignment_id not in assignment_by_id
    )
    if extra_assignment_ids:
        extra_assignments = SampleAssignment.query.filter(
            SampleAssignment.id.in_(extra_assignment_ids)
        ).all()
        assignment_by_id.update({a.id: a for a in extra_assignments})

    prelim_by_id = {event.id: event for event in prelim_events}
    prelim_duplicate_keys = {}
    for event in prelim_events:
        key = (
            event.sample_id, event.assignment_id, event.reviewer_id,
            event.reviewed_at.isoformat() if event.reviewed_at else None,
            event.comments or '',
        )
        prelim_duplicate_keys.setdefault(key, []).append(event.id)

        row = rows.get(event.sample_id)
        if not row:
            continue
        assignment = assignment_by_id.get(event.assignment_id)
        analyst_name = 'not determinable from available records'
        if assignment and assignment.chemist:
            analyst_name = assignment.chemist.full_name
            row['analysts'].add(analyst_name)
        elif event.assignment_id:
            add_issue(
                event.sample_id, 'Incomplete record', 'ReviewHistory',
                event.id, 'Preliminary return has no resolvable assignment/analyst link',
            )
        else:
            add_issue(
                event.sample_id, 'Incomplete record', 'ReviewHistory',
                event.id, 'Preliminary return is missing assignment_id; analyst is not determinable',
            )

        row['preliminary_returns'] += 1
        row['preliminary_return_ids'].append(event.id)
        row['audit_events'].append(
            f'ReviewHistory#{event.id} preliminary returned '
            f'({analyst_name}; {_qa_format_dt(event.reviewed_at)})'
        )
        entry = analyst_entry(analyst_name)
        entry['samples'].add(event.sample_id)
        entry['preliminary_returns'] += 1
        entry['combined_total'] += 1
        entry['event_ids'].append(f'RH#{event.id}')

    for dup_ids in prelim_duplicate_keys.values():
        if len(dup_ids) > 1:
            first = prelim_by_id.get(dup_ids[0])
            if first:
                add_issue(
                    first.sample_id, 'Possible duplicate', 'ReviewHistory',
                    ','.join(str(i) for i in dup_ids),
                    'Multiple Preliminary Review return rows share the same assignment, reviewer, time, and comments',
                )

    doc_by_id = {doc.id: doc for doc in resubmissions}
    doc_duplicate_keys = {}
    for doc in resubmissions:
        row = rows.get(doc.sample_id)
        if not row:
            continue
        raw_rtype = doc.resubmission_type
        rtype = raw_rtype or 'unspecified'
        if raw_rtype and raw_rtype not in type_keys:
            add_issue(
                doc.sample_id, 'Ambiguous record', 'DocumentVersion',
                doc.id, f'Unexpected resubmission type "{raw_rtype}"; counted as unspecified',
            )
            rtype = 'unspecified'
        doc_duplicate_keys.setdefault(
            (doc.sample_id, doc.assignment_id, doc.version_number, doc.file_path, raw_rtype),
            [],
        ).append(doc.id)

        if doc.assignment_id:
            linked_assignment = assignment_by_id.get(doc.assignment_id)
            if linked_assignment and linked_assignment.sample_id != doc.sample_id:
                add_issue(
                    doc.sample_id, 'Conflicting record', 'DocumentVersion',
                    doc.id, 'Document sample_id conflicts with linked assignment sample_id',
                )
            if linked_assignment and linked_assignment.chemist:
                row['analysts'].add(linked_assignment.chemist.full_name)
                analyst_name = linked_assignment.chemist.full_name
            else:
                analyst_name = 'not determinable from available records'
        else:
            analyst_name = 'not determinable from available records'
            add_issue(
                doc.sample_id, 'Incomplete record', 'DocumentVersion',
                doc.id, 'Resubmission row is missing assignment_id; analyst is not determinable',
            )

        if not raw_rtype:
            add_issue(
                doc.sample_id, 'Ambiguous record', 'DocumentVersion',
                doc.id, 'Resubmission type is missing; counted as unspecified',
            )

        row['type_breakdown'][rtype] += 1
        audit = (
            f'DocumentVersion#{doc.id} {type_labels.get(rtype, rtype.title())} '
            f'resubmission upload ({analyst_name}; version {doc.version_number})'
        )
        row['audit_events'].append(audit)

        if rtype == 'preliminary':
            exclusions.append({
                'sample_id': doc.sample_id,
                'lab_number': row['lab_number'],
                'source': 'DocumentVersion',
                'event_id': doc.id,
                'reason': (
                    'Preliminary resubmission upload excluded from combined total; '
                    'Preliminary Review returns are counted from ReviewHistory return events'
                ),
            })
            continue

        row['other_resubmissions'] += 1
        row['other_resubmission_ids'].append(doc.id)
        entry = analyst_entry(analyst_name)
        entry['samples'].add(doc.sample_id)
        entry['other_resubmissions'] += 1
        entry['combined_total'] += 1
        entry['event_ids'].append(f'DV#{doc.id}')

    for dup_ids in doc_duplicate_keys.values():
        if len(dup_ids) > 1:
            first = doc_by_id.get(dup_ids[0])
            if first:
                add_issue(
                    first.sample_id, 'Possible duplicate', 'DocumentVersion',
                    ','.join(str(i) for i in dup_ids),
                    'Multiple resubmission rows share the same assignment, version, file, and type',
                )

    sample_rows = []
    for row in rows.values():
        row['combined_total'] = row['preliminary_returns'] + row['other_resubmissions']
        row['analysts_display'] = _qa_unique_csv(row['analysts'])
        row['audit_trail'] = '; '.join(row['audit_events']) or 'No counted return/resubmission events'
        row['has_quality_flags'] = bool(row['quality_flags'])
        row['quality_flags_display'] = '; '.join(row['quality_flags']) or 'None'
        sample_rows.append(row)

    sample_rows.sort(key=lambda r: r['lab_number'])
    analyst_breakdown = []
    for entry in analyst_data.values():
        analyst_breakdown.append({
            'analyst': entry['analyst'],
            'sample_count': len(entry['samples']),
            'preliminary_returns': entry['preliminary_returns'],
            'other_resubmissions': entry['other_resubmissions'],
            'combined_total': entry['combined_total'],
            'audit_event_ids': ', '.join(entry['event_ids']),
        })
    analyst_breakdown.sort(key=lambda r: r['analyst'].lower())

    totals = {
        'samples': len(sample_rows),
        'samples_with_prelim_returns': sum(1 for r in sample_rows if r['preliminary_returns'] > 0),
        'preliminary_returns': sum(r['preliminary_returns'] for r in sample_rows),
        'other_resubmissions': sum(r['other_resubmissions'] for r in sample_rows),
        'combined_total': sum(r['combined_total'] for r in sample_rows),
    }
    return {
        'sample_rows': sample_rows,
        'analyst_breakdown': analyst_breakdown,
        'quality_issues': quality_issues,
        'exclusions': exclusions,
        'totals': totals,
    }


def _qa_reviewer_stats(assignment_ids):
    """Return a list of per-reviewer dicts for preliminary reviews.

    Queries ReviewHistory for all preliminary-type reviews on the given
    assignment IDs and aggregates totals per reviewer.  Returns a list of
    dicts (sorted by reviewer name) with keys:
        name, total, approved, returned, not_accepted, return_rate, reviews
    where ``reviews`` is a list of report-detail dicts for the modal drill-down.
    """
    if not assignment_ids:
        return []
    rows = (
        ReviewHistory.query
        .filter(
            ReviewHistory.assignment_id.in_(assignment_ids),
            ReviewHistory.review_type == 'preliminary',
            ReviewHistory.action.in_(['approved', 'returned', 'not_accepted']),
        )
        .with_entities(
            ReviewHistory.reviewer_id,
            ReviewHistory.action,
            ReviewHistory.assignment_id,
            ReviewHistory.reviewed_at,
        )
        .all()
    )

    # Bulk-load assignment data so we can look up sample lab numbers and analyst names
    asgn_map = {}
    if rows:
        asgn_ids_needed = {r.assignment_id for r in rows if r.assignment_id}
        asgns = SampleAssignment.query.filter(SampleAssignment.id.in_(asgn_ids_needed)).all()
        for a in asgns:
            asgn_map[a.id] = a

    # Collect reviewer ids then bulk-load names
    reviewer_ids = {r.reviewer_id for r in rows}
    from app.models import User as _User
    users = {u.id: u.full_name for u in _User.query.filter(_User.id.in_(reviewer_ids)).all()}

    data = {}
    for row in rows:
        rid = row.reviewer_id
        if rid not in data:
            data[rid] = {
                'name': users.get(rid, 'Unknown'),
                'total': 0,
                'approved': 0,
                'returned': 0,
                'not_accepted': 0,
                'reviews': [],
            }
        data[rid]['total'] += 1
        if row.action == 'approved':
            data[rid]['approved'] += 1
        elif row.action == 'not_accepted':
            data[rid]['not_accepted'] += 1
        else:
            data[rid]['returned'] += 1

        # Build a report-detail entry for the modal drill-down
        asgn = asgn_map.get(row.assignment_id)
        if asgn:
            sample = asgn.sample
            analyst_name = asgn.chemist.full_name if asgn.chemist else 'Unknown'
            lab_number = sample.lab_number if sample else '—'
            test_name = asgn.test_name or '—'
        else:
            analyst_name = '—'
            lab_number = '—'
            test_name = '—'
        data[rid]['reviews'].append({
            'lab_number': lab_number,
            'analyst': analyst_name,
            'test_name': test_name,
            'action': row.action,
            'reviewed_at': row.reviewed_at.strftime('%Y-%m-%d %H:%M') if row.reviewed_at else '',
            '_sort_dt': row.reviewed_at or datetime.min,
        })

    result = sorted(data.values(), key=lambda x: x['name'].lower())
    for entry in result:
        t = entry['total']
        entry['return_rate'] = round((entry['returned'] + entry['not_accepted']) / t * 100, 1) if t else 0.0
        # Sort reviews newest-first for the modal using the raw datetime, then drop the sort key
        entry['reviews'].sort(key=lambda r: r['_sort_dt'], reverse=True)
        for r in entry['reviews']:
            del r['_sort_dt']
    return result


def _qa_return_reason_summary(analyst_id, assignment_ids):
    """Return a concise text summary of return reasons for an analyst.

    Reads ReviewHistory rows for the given assignment IDs where the action is
    'returned'.  Groups comment keywords into standard QA categories and
    returns a comma-separated summary string.  If no comments are found,
    returns an empty string.
    """
    if not assignment_ids:
        return ''
    rows = (
        ReviewHistory.query
        .filter(
            ReviewHistory.assignment_id.in_(assignment_ids),
            ReviewHistory.action == 'returned',
        )
        .with_entities(ReviewHistory.comments, ReviewHistory.checklist_data)
        .all()
    )

    # Collect all comment text (both free-text comments and checklist JSON)
    import json as _json
    raw_texts = []
    for comments, checklist_data in rows:
        if comments:
            raw_texts.append(_normalize_comment_text(comments))
        if checklist_data:
            try:
                obj = _json.loads(checklist_data)
                # Checklist is typically {label: bool/str}; collect failed item labels
                if isinstance(obj, dict):
                    for label, val in obj.items():
                        if val is False or val == 'no' or val == 'fail' or val == 'failed':
                            raw_texts.append(_normalize_comment_text(label))
                        elif isinstance(val, str) and val.strip():
                            raw_texts.append(_normalize_comment_text(val))
            except (ValueError, TypeError):
                pass

    if not raw_texts:
        return ''

    combined = ' '.join(raw_texts)

    # Keyword → category mapping (order matters: first match wins per keyword)
    keyword_categories = [
        (['calculat', 'arithmetic', 'math', 'formula'], 'Missing/incorrect calculations'),
        (['unit', 'measurement', 'mg', 'g/l', 'ppm', 'ppb', '%'], 'Incorrect units'),
        (['method', 'procedure', 'protocol', 'technique', 'analyt'], 'Incomplete methodology'),
        (['typo', 'spelling', 'grammatical', 'typograph', 'error in text'], 'Typographical errors'),
        (['attach', 'document', 'missing file', 'appendix', 'enclos'], 'Missing attachments'),
        (['transcri', 'data entry', 'recorded', 'copy', 'transfer'], 'Data transcription errors'),
        (['signature', 'sign off', 'initiall', 'unsigned'], 'Missing signature/sign-off'),
        (['incomplete', 'missing', 'omit', 'not filled', 'blank'], 'Incomplete fields'),
        (['reference', 'standard', 'spec', 'limit', 'criteria'], 'Incorrect reference/specification'),
    ]

    found = []
    for keywords, label in keyword_categories:
        if _any_keyword_in_text(keywords, combined):
            found.append(label)

    return '; '.join(found) if found else 'See review comments'


def _prelim_comment_category_breakdown(assignment_ids):
    """Return per-category counts for preliminary review return comments.

    Queries all ReviewHistory rows with review_type='preliminary' and
    action in ('returned', 'not_accepted') for the given assignment IDs,
    then classifies each row's comments against PRELIM_COMMENT_CATEGORIES
    using the same normalized keyword matching as _qa_return_reason_summary.

    Both 'returned' (Return for Correction) and 'not_accepted' (Not
    Accepted - Reject Report) are included: both actions record reviewer
    comments explaining what is wrong with a report, and excluding
    'not_accepted' rows was causing categories that reviewers predominantly
    flag via that decision (e.g. calculation, unit, and typographical
    errors) to show zero selections even though matching records existed.

    Returns a list of dicts (one per category, in PRELIM_COMMENT_CATEGORIES
    order):
        category  – display label
        count     – number of return events matching this category
        pct       – percentage of total matched selections (0.0 when total is 0)
        reviews   – list of per-record dicts for drill-down:
                    {lab_number, analyst, test_name, action, reviewed_at}
    """
    if not assignment_ids:
        return [
            {'category': lbl, 'count': 0, 'pct': 0.0, 'reviews': []}
            for _, lbl in PRELIM_COMMENT_CATEGORIES
        ]

    rows = (
        ReviewHistory.query
        .filter(
            ReviewHistory.assignment_id.in_(assignment_ids),
            ReviewHistory.review_type == 'preliminary',
            ReviewHistory.action.in_(['returned', 'not_accepted']),
        )
        .with_entities(
            ReviewHistory.id,
            ReviewHistory.assignment_id,
            ReviewHistory.comments,
            ReviewHistory.checklist_data,
            ReviewHistory.reviewed_at,
            ReviewHistory.action,
        )
        .all()
    )

    # Bulk-load assignments for drill-down labels
    asgn_ids_needed = {r.assignment_id for r in rows if r.assignment_id}
    asgn_map = {}
    if asgn_ids_needed:
        asgns = SampleAssignment.query.filter(
            SampleAssignment.id.in_(asgn_ids_needed)
        ).all()
        asgn_map = {a.id: a for a in asgns}

    import json as _json

    # Accumulate drill-down records per category label
    cat_reviews = {lbl: [] for _, lbl in PRELIM_COMMENT_CATEGORIES}

    for row in rows:
        raw_texts = []
        if row.comments:
            raw_texts.append(_normalize_comment_text(row.comments))
        if row.checklist_data:
            try:
                obj = _json.loads(row.checklist_data)
                if isinstance(obj, dict):
                    for label, val in obj.items():
                        if val is False or val in ('no', 'fail', 'failed'):
                            raw_texts.append(_normalize_comment_text(label))
                        elif isinstance(val, str) and val.strip():
                            raw_texts.append(_normalize_comment_text(val))
            except (ValueError, TypeError):
                pass

        combined = ' '.join(raw_texts)

        asgn = asgn_map.get(row.assignment_id)
        if asgn:
            sample = asgn.sample
            analyst_name = asgn.chemist.full_name if asgn.chemist else 'Unknown'
            lab_number = sample.lab_number if sample else '—'
            test_name = asgn.test_name or '—'
        else:
            analyst_name = '—'
            lab_number = '—'
            test_name = '—'

        detail = {
            'lab_number': lab_number,
            'analyst': analyst_name,
            'test_name': test_name,
            'action': row.action,
            'reviewed_at': (
                row.reviewed_at.strftime('%Y-%m-%d %H:%M')
                if row.reviewed_at else ''
            ),
        }

        for keywords, lbl in PRELIM_COMMENT_CATEGORIES:
            if _any_keyword_in_text(keywords, combined):
                cat_reviews[lbl].append(detail)

    total = sum(len(v) for v in cat_reviews.values())

    result = []
    for _, lbl in PRELIM_COMMENT_CATEGORIES:
        count = len(cat_reviews[lbl])
        pct = round(count / total * 100, 1) if total > 0 else 0.0
        result.append({'category': lbl, 'count': count, 'pct': pct,
                        'reviews': cat_reviews[lbl]})
    return result


def _qa_analyst_prelim_review_details(analyst_data):
    """Populate a 'reviews' list on each entry in analyst_data.

    For each analyst (keyed by chemist_id) fetches every preliminary review
    event recorded against their assignments so the dashboard can open a
    drill-down modal when a row is clicked.

    Each reviews entry is a plain dict:
        {lab_number, reviewer, test_name, action, reviewed_at}

    The analyst_data dict is mutated in-place (adding key 'reviews').
    """
    all_asgn_ids = [
        aid
        for entry in analyst_data.values()
        for aid in entry.get('assignment_ids', [])
    ]

    # Initialise empty lists so every entry has the key regardless
    for entry in analyst_data.values():
        entry['reviews'] = []

    if not all_asgn_ids:
        return

    # Reverse map: assignment_id → chemist_id
    asgn_to_chemist = {
        aid: cid
        for cid, entry in analyst_data.items()
        for aid in entry.get('assignment_ids', [])
    }

    # Bulk-load assignments for lab number / test name
    asgns = SampleAssignment.query.filter(
        SampleAssignment.id.in_(all_asgn_ids)
    ).all()
    asgn_map = {a.id: a for a in asgns}

    rows = (
        ReviewHistory.query
        .filter(
            ReviewHistory.assignment_id.in_(all_asgn_ids),
            ReviewHistory.review_type == 'preliminary',
            ReviewHistory.action.in_(['approved', 'returned', 'not_accepted']),
        )
        .with_entities(
            ReviewHistory.assignment_id,
            ReviewHistory.reviewer_id,
            ReviewHistory.action,
            ReviewHistory.reviewed_at,
        )
        .all()
    )

    # Bulk-load reviewer names
    reviewer_ids = {r.reviewer_id for r in rows}
    from app.models import User as _User
    users = {
        u.id: u.full_name
        for u in _User.query.filter(_User.id.in_(reviewer_ids)).all()
    }

    for row in rows:
        cid = asgn_to_chemist.get(row.assignment_id)
        if cid is None:
            continue
        entry = analyst_data.get(cid)
        if entry is None:
            continue

        asgn = asgn_map.get(row.assignment_id)
        if asgn:
            sample = asgn.sample
            lab_number = sample.lab_number if sample else '—'
            test_name = asgn.test_name or '—'
        else:
            lab_number = '—'
            test_name = '—'

        entry['reviews'].append({
            'lab_number': lab_number,
            'reviewer': users.get(row.reviewer_id, 'Unknown'),
            'test_name': test_name,
            'action': row.action,
            'reviewed_at': (
                row.reviewed_at.strftime('%Y-%m-%d %H:%M')
                if row.reviewed_at else ''
            ),
            '_sort_dt': row.reviewed_at or datetime.min,
        })

    # Sort newest-first and drop the internal sort key
    for entry in analyst_data.values():
        entry['reviews'].sort(key=lambda r: r['_sort_dt'], reverse=True)
        for r in entry['reviews']:
            del r['_sort_dt']


def _qa_preliminary_analyst_stats(assignments):
    """Return per-analyst preliminary review stats for the QA Performance report.

    For each analyst computes per-sample counts and timing metrics for
    preliminary reviews that resulted in 'returned' or 'not_accepted' actions.

    Returns a dict keyed by analyst chemist_id with keys:
        name, qty_samples, lab_type,
        returned_count, not_accepted_count,
        avg_time_returned_hrs, avg_time_not_accepted_hrs,
        total_time_returned_hrs, total_time_not_accepted_hrs
    """
    if not assignments:
        return {}

    # Build lookup: assignment_id → (chemist_id, chemist_name, report_submitted_at, sample_id, lab_type)
    assign_map = {}
    for a in assignments:
        assign_map[a.id] = {
            'chemist_id': a.chemist_id,
            'chemist_name': a.chemist.full_name if a.chemist else 'Unknown',
            'report_submitted_at': a.report_submitted_at,
            'sample_id': a.sample_id,
            'lab_type': _qa_branch_category(a.sample.sample_type) if a.sample else None,
        }

    assignment_ids = list(assign_map.keys())

    # Pre-populate per-analyst data from ALL assignments so that qty_samples
    # reflects every unique sample assigned to the analyst, not only those
    # that appear in return events.
    data = {}
    for aid, ainfo in assign_map.items():
        cid = ainfo['chemist_id']
        lab_type = ainfo['lab_type']
        if cid not in data:
            data[cid] = {
                'name': ainfo['chemist_name'],
                'lab_type': lab_type or '—',
                'sample_ids': set(),
                'returned_count': 0,
                'not_accepted_count': 0,
                '_returned_durations': [],
                '_not_accepted_durations': [],
            }
        data[cid]['sample_ids'].add(ainfo['sample_id'])

    # Fetch all preliminary review entries with action returned or not_accepted.
    # Each ReviewHistory row is one distinct return event (identified by its id);
    # do NOT deduplicate by analyst-sample pair — multiple returns of the same
    # sample are all counted individually per the QA metric definition.
    rows = (
        ReviewHistory.query
        .filter(
            ReviewHistory.assignment_id.in_(assignment_ids),
            ReviewHistory.review_type == 'preliminary',
            ReviewHistory.action.in_(['returned', 'not_accepted']),
        )
        .with_entities(
            ReviewHistory.id,
            ReviewHistory.assignment_id,
            ReviewHistory.action,
            ReviewHistory.reviewed_at,
        )
        .order_by(ReviewHistory.reviewed_at.asc())
        .all()
    )

    for row in rows:
        ainfo = assign_map.get(row.assignment_id)
        if not ainfo:
            continue
        cid = ainfo['chemist_id']

        entry = data.get(cid)
        if not entry:
            continue

        # Calculate duration (hours) if report_submitted_at is available
        duration_hrs = None
        if ainfo['report_submitted_at'] and row.reviewed_at:
            delta = row.reviewed_at - ainfo['report_submitted_at']
            duration_hrs = delta.total_seconds() / 3600.0

        if row.action == 'returned':
            entry['returned_count'] += 1
            if duration_hrs is not None:
                entry['_returned_durations'].append(duration_hrs)
        else:  # not_accepted
            entry['not_accepted_count'] += 1
            if duration_hrs is not None:
                entry['_not_accepted_durations'].append(duration_hrs)

    # Compute averages and totals; clean up internal accumulators
    result = {}
    for cid, entry in data.items():
        ret_durs = entry.pop('_returned_durations')
        na_durs = entry.pop('_not_accepted_durations')

        total_ret = sum(ret_durs) if ret_durs else 0.0
        total_na = sum(na_durs) if na_durs else 0.0
        avg_ret = round(total_ret / len(ret_durs), 2) if ret_durs else 0.0
        avg_na = round(total_na / len(na_durs), 2) if na_durs else 0.0

        entry['qty_samples'] = len(entry['sample_ids'])
        entry.pop('sample_ids')
        entry['avg_time_returned_hrs'] = avg_ret
        entry['avg_time_not_accepted_hrs'] = avg_na
        entry['total_time_returned_hrs'] = round(total_ret, 2)
        entry['total_time_not_accepted_hrs'] = round(total_na, 2)
        result[cid] = entry

    return result


@main_bp.route('/reports/qa-performance')
@login_required
def qa_performance_summary():
    """QA Performance Summary: per-analyst breakdown by lab category, acceptance, and return reasons."""
    if not _can_view_qa_performance():
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int, default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)  # 0 = full year
    month = request.args.get('month', type=int, default=0)       # 0 = all months
    search = request.args.get('search', '').strip()

    count_by = Setting.get(QA_PERFORMANCE_COUNT_BY_KEY, 'sample')

    # Base query: all assignments within the fiscal period
    q = (
        SampleAssignment.query
        .join(Sample, SampleAssignment.sample_id == Sample.id)
    )
    if month and 1 <= month <= 12:
        # Month filter: full fiscal year but restrict to the selected calendar month
        fy_start, fy_end = fiscal_year_date_range(year, None)
        q = q.filter(
            SampleAssignment.assigned_date >= fy_start,
            SampleAssignment.assigned_date <= fy_end,
            sa_extract('month', SampleAssignment.assigned_date) == month,
        )
    else:
        q = _fiscal_year_filter(q, SampleAssignment.assigned_date, year,
                                quarter if quarter in (1, 2, 3, 4) else None)

    assignments = q.order_by(SampleAssignment.assigned_date.desc()).all()
    corrected_report = _qa_corrected_sample_report(assignments)

    # Fetch per-assignment preliminary return counts from ReviewHistory (authoritative source).
    # Using ReviewHistory.action == 'returned' is more accurate than
    # counting DocumentVersion resubmissions, because it captures samples that have been
    # returned but not yet resubmitted by the analyst.
    all_ids = [a.id for a in assignments]
    prelim_counts = _preliminary_return_counts_for_assignments(all_ids)

    # Build per-analyst data
    analyst_data = {}
    for a in assignments:
        cid = a.chemist_id
        if cid not in analyst_data:
            analyst_data[cid] = {
                'id': cid,
                'name': a.chemist.full_name if a.chemist else 'Unknown',
                'food': 0,
                'tox': 0,
                'pharm': 0,
                'accepted_first': 0,
                'returned': 0,
                'assignment_ids': [],
                '_sample_map': {},  # used only when count_by == 'sample'
            }
        entry = analyst_data[cid]
        entry['assignment_ids'].append(a.id)

        if count_by == 'sample':
            # Accumulate per-sample data; stats computed after the loop
            sid = a.sample_id
            if sid not in entry['_sample_map']:
                entry['_sample_map'][sid] = {'sample': a.sample, 'assignment_ids': []}
            entry['_sample_map'][sid]['assignment_ids'].append(a.id)
        else:
            # Test-based counting (original behaviour)
            cat = _qa_branch_category(a.sample.sample_type)
            if cat:
                entry[cat] += 1
            n_prelim = prelim_counts.get(a.id, 0)
            if n_prelim == 0:
                entry['accepted_first'] += 1
            else:
                entry['returned'] += 1

    # For sample-based counting, roll up the per-sample stats into analyst totals
    if count_by == 'sample':
        for entry in analyst_data.values():
            for sdata in entry['_sample_map'].values():
                cat = _qa_branch_category(sdata['sample'].sample_type)
                if cat:
                    entry[cat] += 1
                any_returned = any(prelim_counts.get(aid, 0) > 0
                                   for aid in sdata['assignment_ids'])
                if any_returned:
                    entry['returned'] += 1
                else:
                    entry['accepted_first'] += 1
            del entry['_sample_map']
    else:
        for entry in analyst_data.values():
            del entry['_sample_map']

    # Compute return reason summary per analyst
    for entry in analyst_data.values():
        entry['return_reasons'] = _qa_return_reason_summary(
            entry['id'], entry['assignment_ids']
        )

    # Populate preliminary review drill-down records for each analyst
    _qa_analyst_prelim_review_details(analyst_data)

    # Sort by analyst name
    analyst_list = sorted(analyst_data.values(), key=lambda x: x['name'].lower())

    # Optional name search
    if search:
        analyst_list = [a for a in analyst_list if search.lower() in a['name'].lower()]

    available_years = _available_fiscal_years()

    # Grand totals row
    totals = {
        'food': sum(a['food'] for a in analyst_list),
        'tox': sum(a['tox'] for a in analyst_list),
        'pharm': sum(a['pharm'] for a in analyst_list),
        'accepted_first': sum(a['accepted_first'] for a in analyst_list),
        'returned': sum(a['returned'] for a in analyst_list),
    }

    # Preliminary reviewer statistics
    reviewer_stats = _qa_reviewer_stats(all_ids)

    # Per-analyst preliminary review performance stats (returned / not_accepted with timing)
    prelim_analyst_stats = _qa_preliminary_analyst_stats(assignments)
    prelim_analyst_list = sorted(prelim_analyst_stats.values(), key=lambda x: x['name'].lower())
    if search:
        prelim_analyst_list = [a for a in prelim_analyst_list if search.lower() in a['name'].lower()]

    # Preliminary review comment category breakdown
    comment_category_breakdown = _prelim_comment_category_breakdown(all_ids)

    return render_template(
        'qa_performance.html',
        analyst_list=analyst_list,
        year=year,
        quarter=quarter,
        month=month,
        search=search,
        available_years=available_years,
        totals=totals,
        count_by=count_by,
        corrected_report=corrected_report,
        corrected_report_preview_rows=corrected_report['sample_rows'][:10],
        reviewer_stats=reviewer_stats,
        prelim_analyst_list=prelim_analyst_list,
        comment_category_breakdown=comment_category_breakdown,
    )


@main_bp.route('/reports/qa-performance/download')
@login_required
def qa_performance_download():
    """Download the QA Performance Summary as a CSV file."""
    if not _can_view_qa_performance():
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int, default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)

    count_by = Setting.get(QA_PERFORMANCE_COUNT_BY_KEY, 'sample')

    q = (
        SampleAssignment.query
        .join(Sample, SampleAssignment.sample_id == Sample.id)
    )
    q = _fiscal_year_filter(q, SampleAssignment.assigned_date, year,
                            quarter if quarter in (1, 2, 3, 4) else None)
    assignments = q.order_by(SampleAssignment.assigned_date.desc()).all()
    corrected_report = _qa_corrected_sample_report(assignments)

    all_ids = [a.id for a in assignments]
    prelim_counts = _preliminary_return_counts_for_assignments(all_ids)

    analyst_data = {}
    for a in assignments:
        cid = a.chemist_id
        if cid not in analyst_data:
            analyst_data[cid] = {
                'name': a.chemist.full_name if a.chemist else 'Unknown',
                'food': 0, 'tox': 0, 'pharm': 0,
                'accepted_first': 0, 'returned': 0,
                'assignment_ids': [],
                '_sample_map': {},
            }
        entry = analyst_data[cid]
        entry['assignment_ids'].append(a.id)

        if count_by == 'sample':
            sid = a.sample_id
            if sid not in entry['_sample_map']:
                entry['_sample_map'][sid] = {'sample': a.sample, 'assignment_ids': []}
            entry['_sample_map'][sid]['assignment_ids'].append(a.id)
        else:
            cat = _qa_branch_category(a.sample.sample_type)
            if cat:
                entry[cat] += 1
            n_prelim = prelim_counts.get(a.id, 0)
            if n_prelim == 0:
                entry['accepted_first'] += 1
            else:
                entry['returned'] += 1

    if count_by == 'sample':
        for entry in analyst_data.values():
            for sdata in entry['_sample_map'].values():
                cat = _qa_branch_category(sdata['sample'].sample_type)
                if cat:
                    entry[cat] += 1
                any_returned = any(prelim_counts.get(aid, 0) > 0
                                   for aid in sdata['assignment_ids'])
                if any_returned:
                    entry['returned'] += 1
                else:
                    entry['accepted_first'] += 1
            del entry['_sample_map']
    else:
        for entry in analyst_data.values():
            del entry['_sample_map']

    for entry in analyst_data.values():
        entry['return_reasons'] = _qa_return_reason_summary(None, entry['assignment_ids'])

    rows = sorted(analyst_data.values(), key=lambda x: x['name'].lower())

    # Preliminary reviewer statistics
    reviewer_stats = _qa_reviewer_stats(all_ids)

    # Per-analyst preliminary review performance stats
    prelim_analyst_stats = _qa_preliminary_analyst_stats(assignments)
    prelim_rows = sorted(prelim_analyst_stats.values(), key=lambda x: x['name'].lower())

    count_label = 'samples' if count_by == 'sample' else 'tests'
    buf = io.StringIO()
    writer = csv.writer(buf)
    fy_label = f'FY {year}/{year + 1}'
    q_label = f' Q{quarter}' if quarter in (1, 2, 3, 4) else ''
    writer.writerow([f'QA Performance Summary — {fy_label}{q_label}'])
    writer.writerow([f'Counts based on: {count_label}'])
    writer.writerow(['Reconciliation'])
    writer.writerow(['Previous figures could be inaccurate when they collapsed a sample to returned/not returned.'])
    writer.writerow(['Previous figures could also be inaccurate when they counted report uploads instead of ReviewHistory return events.'])
    writer.writerow(['Exact prior figures are not determinable from available records.'])
    writer.writerow([])
    writer.writerow(['Corrected Sample-Level Summary'])
    writer.writerow(['Unique Samples', corrected_report['totals']['samples']])
    writer.writerow(['Samples with Preliminary Review Returns', corrected_report['totals']['samples_with_prelim_returns']])
    writer.writerow(['Preliminary Review Return Events', corrected_report['totals']['preliminary_returns']])
    writer.writerow(['Other Resubmission Events', corrected_report['totals']['other_resubmissions']])
    writer.writerow(['Combined Return/Resubmission Events', corrected_report['totals']['combined_total']])
    writer.writerow([])
    writer.writerow(['Sample-Level Return Count and Audit Trail'])
    writer.writerow([
        'Sample ID', 'Sample Name', 'Laboratory', 'Analysts Involved',
        'Preliminary Review Returns',
        'Preliminary Resubmission Uploads (excluded from combined total)',
        'Senior Chemist Review Resubmissions', 'Deputy Review Resubmissions',
        'HOD Review Resubmissions', 'Unspecified Resubmissions',
        'Other Resubmissions', 'Combined Total',
        'Preliminary Return ReviewHistory IDs', 'Other Resubmission DocumentVersion IDs',
        'Record-Level Audit Trail', 'Data Quality Flags',
    ])
    for row in corrected_report['sample_rows']:
        bdown = row['type_breakdown']
        writer.writerow([
            row['lab_number'], row['sample_name'], row['lab_type'], row['analysts_display'],
            row['preliminary_returns'], bdown.get('preliminary', 0),
            bdown.get('technical', 0), bdown.get('deputy', 0),
            bdown.get('hod', 0), bdown.get('unspecified', 0),
            row['other_resubmissions'], row['combined_total'],
            ', '.join(str(i) for i in row['preliminary_return_ids']),
            ', '.join(str(i) for i in row['other_resubmission_ids']),
            row['audit_trail'], row['quality_flags_display'],
        ])
    writer.writerow([])
    writer.writerow(['Breakdown by Analyst'])
    writer.writerow([
        'Analyst', 'Samples Involved', 'Preliminary Review Returns',
        'Other Resubmissions', 'Combined Total', 'Audit Event IDs',
    ])
    for row in corrected_report['analyst_breakdown']:
        writer.writerow([
            row['analyst'], row['sample_count'], row['preliminary_returns'],
            row['other_resubmissions'], row['combined_total'], row['audit_event_ids'],
        ])
    writer.writerow([])
    writer.writerow(['Duplicates, Exclusions, and Data-Quality Issues'])
    writer.writerow(['Sample ID', 'Source', 'Event/Row ID', 'Type', 'Detail'])
    for row in corrected_report['exclusions']:
        writer.writerow([
            row['lab_number'], row['source'], row['event_id'], 'Excluded',
            row['reason'],
        ])
    for row in corrected_report['quality_issues']:
        writer.writerow([
            row['lab_number'], row['source'], row['event_id'], row['issue_type'],
            row['detail'],
        ])
    if not corrected_report['exclusions'] and not corrected_report['quality_issues']:
        writer.writerow(['not determinable from available records', '', '', 'None flagged', 'No duplicate, conflicting, incomplete, or ambiguous records detected by report rules'])
    writer.writerow([])
    writer.writerow(['Legacy Analyst Summary (recalculated with Preliminary Review returns from ReviewHistory only)'])
    writer.writerow([
        'Analyst', 'Food', 'Tox', 'Pharm',
        'Accepted on First Submission', 'Returned for Correction',
        'Comments / Summary of Reasons for Returned Report',
    ])
    for row in rows:
        writer.writerow([
            row['name'], row['food'], row['tox'], row['pharm'],
            row['accepted_first'], row['returned'], row['return_reasons'],
        ])
    # Totals row
    writer.writerow([
        'TOTAL',
        sum(r['food'] for r in rows),
        sum(r['tox'] for r in rows),
        sum(r['pharm'] for r in rows),
        sum(r['accepted_first'] for r in rows),
        sum(r['returned'] for r in rows),
        '',
    ])

    # Preliminary Review Performance (per-analyst timing stats)
    if prelim_rows:
        writer.writerow([])
        writer.writerow(['Preliminary Review Performance — Per Analyst'])
        writer.writerow([
            'Analyst', 'Lab Type', 'Qty Samples',
            'Returned', 'Not Accepted',
            'Avg Time to Return (hrs)', 'Avg Time to Not Accept (hrs)',
            'Total Time Returned (hrs)', 'Total Time Not Accepted (hrs)',
        ])
        for pr in prelim_rows:
            writer.writerow([
                pr['name'], pr['lab_type'], pr['qty_samples'],
                pr['returned_count'], pr['not_accepted_count'],
                pr['avg_time_returned_hrs'], pr['avg_time_not_accepted_hrs'],
                pr['total_time_returned_hrs'], pr['total_time_not_accepted_hrs'],
            ])
        writer.writerow([
            'TOTAL', '', sum(r['qty_samples'] for r in prelim_rows),
            sum(r['returned_count'] for r in prelim_rows),
            sum(r['not_accepted_count'] for r in prelim_rows),
            '', '',
            round(sum(r['total_time_returned_hrs'] for r in prelim_rows), 2),
            round(sum(r['total_time_not_accepted_hrs'] for r in prelim_rows), 2),
        ])

    # Preliminary Reviewer Stats section
    if reviewer_stats:
        writer.writerow([])
        writer.writerow(['Preliminary Review Activity'])
        writer.writerow([
            'Reviewer', 'Total Reviews', 'Approved', 'Returned', 'Not Accepted', 'Non-Approval Rate (%)',
        ])
        for rv in reviewer_stats:
            writer.writerow([
                rv['name'], rv['total'], rv['approved'],
                rv['returned'], rv['not_accepted'], rv['return_rate'],
            ])

    fname_q = f'_Q{quarter}' if quarter in (1, 2, 3, 4) else ''
    filename = f'QA_Performance_Summary_{year}{fname_q}.csv'
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Non-Working Days Calendar Management
# ---------------------------------------------------------------------------

@main_bp.route('/calendar', methods=['GET', 'POST'])
@login_required
def calendar_management():
    """Calendar interface for managing non-working days (Admin/HOD only)."""
    if not current_user.has_any_role(Role.ADMIN, Role.HOD):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    from app.forms import NonWorkingDayForm
    form = NonWorkingDayForm()

    if form.validate_on_submit():
        existing = NonWorkingDay.query.filter_by(date=form.date.data).first()
        if existing:
            flash('This date is already marked as a non-working day.', 'warning')
        else:
            nwd = NonWorkingDay(
                date=form.date.data,
                description=form.description.data,
                day_type=form.day_type.data,
                created_by=current_user.id,
            )
            db.session.add(nwd)
            db.session.commit()
            flash('Non-working day added.', 'success')
        return redirect(url_for('main.calendar_management'))

    year = request.args.get('year', type=int, default=jamaica_now().year)
    non_working_days = NonWorkingDay.query.filter(
        db.extract('year', NonWorkingDay.date) == year
    ).order_by(NonWorkingDay.date).all()

    return render_template(
        'calendar.html',
        form=form,
        non_working_days=non_working_days,
        year=year,
    )


@main_bp.route('/calendar/<int:nwd_id>/delete', methods=['POST'])
@login_required
def delete_non_working_day(nwd_id):
    """Delete a non-working day entry."""
    if not current_user.has_any_role(Role.ADMIN, Role.HOD):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    nwd = db.get_or_404(NonWorkingDay, nwd_id)
    db.session.delete(nwd)
    db.session.commit()
    flash('Non-working day removed.', 'success')
    return redirect(url_for('main.calendar_management'))


# ---------------------------------------------------------------------------
# Admin Settings
# ---------------------------------------------------------------------------

@main_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    is_admin_or_hod = current_user.has_any_role(Role.ADMIN, Role.HOD)
    can_manage_review = (is_admin_or_hod
                         or current_user.has_permission(Permission.MANAGE_SETTINGS))
    if not can_manage_review:
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    is_admin = current_user.has_role(Role.ADMIN)

    if request.method == 'POST':
        # Review group settings: any user with settings access may change these
        prelim_grouped = 'preliminary_review_grouped' in request.form
        Setting.set('preliminary_review_grouped', str(prelim_grouped).lower())
        technical_grouped = 'technical_review_grouped' in request.form
        Setting.set('technical_review_grouped', str(technical_grouped).lower())

        # Default resubmission type filter for Analyst Reports
        raw_default_resub = request.form.getlist('default_resub_types')
        valid_type_keys = {k for k, _ in RESUBMISSION_TYPES}
        if not raw_default_resub or 'all' in raw_default_resub:
            Setting.set(ANALYST_REPORT_RESUB_TYPES_KEY, 'all')
        else:
            chosen = [t for t in raw_default_resub if t in valid_type_keys]
            Setting.set(ANALYST_REPORT_RESUB_TYPES_KEY, ','.join(chosen) if chosen else 'all')

        # QA Performance Summary — calculation method
        count_by = request.form.get('qa_performance_count_by', 'sample')
        if count_by not in ('sample', 'test'):
            count_by = 'sample'
        Setting.set(QA_PERFORMANCE_COUNT_BY_KEY, count_by)

        # Email notifications and SMTP: admin/HOD only
        if is_admin_or_hod:
            email_enabled = 'email_enabled' in request.form
            Setting.set('email_enabled', str(email_enabled).lower())

            # SMTP settings – admin only
            if is_admin:
                smtp_server = request.form.get('smtp_server', '').strip()
                smtp_port = request.form.get('smtp_port', '587').strip()
                smtp_use_tls = 'smtp_use_tls' in request.form
                smtp_username = request.form.get('smtp_username', '').strip()
                smtp_sender = request.form.get('smtp_sender', '').strip()
                # Only update password if a value was actually submitted (empty means keep existing)
                smtp_password_raw = request.form.get('smtp_password', '')
                Setting.set('smtp_server', smtp_server)
                Setting.set('smtp_port', smtp_port)
                Setting.set('smtp_use_tls', str(smtp_use_tls).lower())
                Setting.set('smtp_username', smtp_username)
                Setting.set('smtp_sender', smtp_sender)
                if smtp_password_raw:
                    Setting.set('smtp_password', smtp_password_raw)

        db.session.add(AuditLog(
            action='SETTINGS_UPDATED',
            entity_type='Setting',
            entity_id=None,
            entity_label='System Settings',
            details=json.dumps({
                'updated_by': current_user.full_name,
            }),
            performed_by=current_user.id,
            performed_at=jamaica_now(),
        ))
        db.session.commit()
        flash('Settings updated.', 'success')
        return redirect(url_for('main.settings'))

    email_enabled = Setting.get_bool('email_enabled', default=True)
    preliminary_review_grouped = Setting.get_bool('preliminary_review_grouped', default=False)
    technical_review_grouped = Setting.get_bool('technical_review_grouped', default=False)
    sample_count = Sample.query.count()
    qa_performance_count_by = Setting.get(QA_PERFORMANCE_COUNT_BY_KEY, 'sample')

    # Default resubmission types for the settings UI
    default_resub_raw = Setting.get(ANALYST_REPORT_RESUB_TYPES_KEY, 'all')
    if default_resub_raw == 'all' or not default_resub_raw:
        default_resub_selected = ['all']
    else:
        default_resub_selected = [t.strip() for t in default_resub_raw.split(',') if t.strip()]

    smtp_settings = None
    if is_admin:
        smtp_settings = {
            'server': Setting.get('smtp_server', ''),
            'port': Setting.get('smtp_port', '587'),
            'use_tls': Setting.get_bool('smtp_use_tls', default=True),
            'username': Setting.get('smtp_username', ''),
            'sender': Setting.get('smtp_sender', ''),
            'has_password': bool(Setting.get('smtp_password', '')),
        }

    return render_template('settings.html',
                           email_enabled=email_enabled,
                           preliminary_review_grouped=preliminary_review_grouped,
                           technical_review_grouped=technical_review_grouped,
                           sample_count=sample_count,
                           smtp_settings=smtp_settings,
                           is_admin_or_hod=is_admin_or_hod,
                           resubmission_types=RESUBMISSION_TYPES,
                           default_resub_selected=default_resub_selected,
                           qa_performance_count_by=qa_performance_count_by)


@main_bp.route('/settings/test-email', methods=['POST'])
@login_required
def test_email():
    """Send a test email to the current user to verify mail configuration."""
    if not current_user.has_any_role(Role.ADMIN, Role.HOD):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))
    if not current_user.email:
        flash('Your account does not have an email address configured.', 'warning')
        return redirect(url_for('main.settings'))
    try:
        from app.notifications import send_email, _build_html_email
        body_text = (
            f'Hello {current_user.first_name},\n\n'
            'This is a test email from DGC SMS to verify your mail configuration is working.\n\n'
            'If you received this email, your email settings are correctly configured.'
        )
        body_html = _build_html_email(
            'Test Email',
            body_text,
        )
        send_email(
            subject='[DGC SMS] Test Email',
            recipients=[current_user.email],
            body_text=body_text,
            body_html=body_html,
        )
        flash(
            f'Test email queued for delivery to {current_user.email}. '
            'Check your inbox (and spam folder) in a few minutes.',
            'success',
        )
    except Exception as exc:
        current_app.logger.exception('Test email failed')
        flash(f'Failed to send test email: {exc}', 'danger')
    return redirect(url_for('main.settings'))


@main_bp.route('/clear-sample-data', methods=['POST'])
@login_required
def clear_sample_data():
    if not current_user.has_role(Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    # Remove uploaded files
    import shutil, os
    upload_folder = current_app.config.get('UPLOAD_FOLDER')
    if upload_folder and os.path.isdir(upload_folder):
        for entry in os.listdir(upload_folder):
            path = os.path.join(upload_folder, entry)
            if os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)

    # Delete in order respecting FK constraints
    Notification.query.filter(
        Notification.link.like('%/samples/%')
    ).delete(synchronize_session=False)
    BackDateRequest.query.delete()
    DocumentVersion.query.delete()
    SampleHistory.query.delete()
    SampleAssignment.query.delete()
    from app.models import SupportingDocument
    SupportingDocument.query.delete()
    Sample.query.delete()
    db.session.commit()

    flash('All sample data has been cleared.', 'success')
    return redirect(url_for('main.settings'))


# ---------------------------------------------------------------------------
# Back-Dating Request & Approval
# ---------------------------------------------------------------------------

@main_bp.route('/backdate-requests')
@login_required
def backdate_requests():
    """View pending back-date requests (HOD/Deputy only)."""
    if not current_user.has_any_role(Role.HOD, Role.DEPUTY, Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    page = request.args.get('page', 1, type=int)
    status_filter = request.args.get('status', 'pending')
    q = BackDateRequest.query
    if status_filter in ('pending', 'approved', 'denied'):
        q = q.filter_by(status=status_filter)
    pagination = q.order_by(BackDateRequest.requested_at.desc()).paginate(
        page=page, per_page=25, error_out=False
    )

    return render_template(
        'backdate_requests.html',
        requests=pagination.items,
        pagination=pagination,
        status_filter=status_filter,
    )


@main_bp.route('/backdate-requests/<int:req_id>/decide', methods=['POST'])
@login_required
def decide_backdate(req_id):
    """Approve or deny a back-date request."""
    if not current_user.has_any_role(Role.HOD, Role.DEPUTY, Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    bdr = db.get_or_404(BackDateRequest, req_id)
    if bdr.status != 'pending':
        flash('This request has already been decided.', 'warning')
        return redirect(url_for('main.backdate_requests'))

    decision = request.form.get('decision')
    comments = request.form.get('comments', '')

    if decision not in ('approved', 'denied'):
        flash('Invalid decision.', 'danger')
        return redirect(url_for('main.backdate_requests'))

    bdr.status = decision
    bdr.decided_by = current_user.id
    bdr.decided_at = jamaica_now()
    bdr.decision_comments = comments

    # If approved, apply the back-dated value
    if decision == 'approved':
        from datetime import datetime as dt
        try:
            new_date = dt.strptime(bdr.proposed_date, '%Y-%m-%d').date()

            # Assignment-level fields
            assignment_fields = {
                'assigned_date', 'expected_completion',
                'report_submitted_at', 'test_date', 'reviewed_at',
            }

            # Sample-level DateTime fields (need to preserve the time)
            sample_datetime_fields = {
                'date_registered', 'deputy_reviewed_at',
                'certificate_prepared_at', 'certified_at',
            }

            if bdr.field_name in assignment_fields and bdr.assignment_id:
                asgn = db.session.get(SampleAssignment, bdr.assignment_id)
                if asgn:
                    if bdr.field_name in (
                        'assigned_date', 'report_submitted_at', 'reviewed_at',
                    ):
                        # DateTime columns – preserve the time
                        old_val = getattr(asgn, bdr.field_name, None)
                        if old_val and hasattr(old_val, 'time'):
                            new_value = dt.combine(new_date, old_val.time())
                        else:
                            new_value = dt.combine(new_date, dt.min.time())
                        setattr(asgn, bdr.field_name, new_value)
                    else:
                        setattr(asgn, bdr.field_name, new_date)
            else:
                # Sample-level fields
                sample = db.session.get(Sample, bdr.sample_id)
                if sample and hasattr(sample, bdr.field_name):
                    if bdr.field_name in sample_datetime_fields:
                        # DateTime columns – preserve the time
                        old_val = getattr(sample, bdr.field_name, None)
                        if old_val and hasattr(old_val, 'time'):
                            new_value = dt.combine(new_date, old_val.time())
                        else:
                            new_value = dt.combine(new_date, dt.min.time())
                        setattr(sample, bdr.field_name, new_value)
                    else:
                        setattr(sample, bdr.field_name, new_date)
        except (ValueError, AttributeError):
            current_app.logger.error(
                'Failed to apply back-date for request %d: field=%s, proposed=%s',
                bdr.id, bdr.field_name, bdr.proposed_date,
            )
            flash('Back-date approved but could not be applied automatically. '
                  'Please update the date manually.', 'warning')

    # Log the decision
    requester_name = bdr.requester.full_name if bdr.requester else 'Unknown'
    db.session.add(SampleHistory(
        sample_id=bdr.sample_id,
        action=f'Back-date request {decision}',
        details=(f'Field: {bdr.field_name}, Original: {bdr.original_date}, '
                 f'Proposed: {bdr.proposed_date}, Decision: {decision}. '
                 f'Requested by: {requester_name}'
                 f'{", Comments: " + comments if comments else ""}'),
        performed_by=current_user.id,
        action_type=f'Back-Date {decision.title()}',
        object_affected='Sample' if not bdr.assignment_id else 'Assignment',
        change_description=(f'{bdr.field_name}: {bdr.original_date} → {bdr.proposed_date} '
                           f'({decision} by {current_user.full_name}, '
                           f'requested by {requester_name})'),
    ))
    audit_action = f'BACKDATE_{decision.upper()}'
    db.session.add(AuditLog(
        action=audit_action,
        entity_type='Sample',
        entity_id=bdr.sample_id,
        entity_label=bdr.sample.lab_number if bdr.sample else str(bdr.sample_id),
        details=json.dumps({
            'lab_number': bdr.sample.lab_number if bdr.sample else None,
            'field': bdr.field_name.replace('_', ' ').title(),
            'original_date': bdr.original_date or None,
            'proposed_date': bdr.proposed_date,
            'decision': decision,
            'requested_by': requester_name,
            'decided_by': current_user.full_name,
            'comments': comments or None,
        }),
        performed_by=current_user.id,
    ))
    db.session.commit()

    from app.notifications import notify_backdate_request_decided
    notify_backdate_request_decided(bdr)
    db.session.commit()

    flash(f'Back-date request {decision}.', 'success')
    return redirect(url_for('main.backdate_requests'))


# ---------------------------------------------------------------------------
# Delete Request Management  (HOD / Admin)
# ---------------------------------------------------------------------------

@main_bp.route('/delete-requests')
@login_required
def delete_requests():
    """View deletion requests – HOD and Admin only."""
    if not current_user.has_any_role(Role.HOD, Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    page = request.args.get('page', 1, type=int)
    status_filter = request.args.get('status', 'pending')
    q = DeleteRequest.query
    if status_filter in ('pending', 'approved', 'denied'):
        q = q.filter_by(status=status_filter)
    pagination = q.order_by(DeleteRequest.requested_at.desc()).paginate(
        page=page, per_page=25, error_out=False
    )

    return render_template(
        'delete_requests.html',
        requests=pagination.items,
        pagination=pagination,
        status_filter=status_filter,
    )


@main_bp.route('/delete-requests/<int:req_id>/decide', methods=['POST'])
@login_required
def decide_delete_request(req_id):
    """Approve or deny a deletion request.  Approval immediately performs the deletion."""
    if not current_user.has_any_role(Role.HOD, Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    dr = db.get_or_404(DeleteRequest, req_id)
    if dr.status != 'pending':
        flash('This request has already been decided.', 'warning')
        return redirect(url_for('main.delete_requests'))

    decision = request.form.get('decision')
    comments = request.form.get('comments', '')

    if decision not in ('approved', 'denied'):
        flash('Invalid decision.', 'danger')
        return redirect(url_for('main.delete_requests'))

    now = jamaica_now()
    dr.status = decision
    dr.decided_by = current_user.id
    dr.decided_at = now
    dr.decision_comments = comments

    if decision == 'approved':
        import json as _json
        if dr.request_type == 'sample' and dr.sample_id:
            sample = db.session.get(Sample, dr.sample_id)
            if sample:
                # Build audit snapshot
                uploader = db.session.get(User, sample.uploaded_by)
                snapshot = _json.dumps({
                    'lab_number': sample.lab_number,
                    'sample_name': sample.sample_name,
                    'sample_type': sample.sample_type.value,
                    'status': sample.status.value,
                    'date_received': sample.date_received.isoformat() if sample.date_received else None,
                    'date_registered': sample.date_registered.isoformat() if sample.date_registered else None,
                    'uploaded_by': sample.uploaded_by,
                    'uploaded_by_name': uploader.full_name if uploader else None,
                    'assignment_count': sample.assignments.count(),
                    'delete_request_id': dr.id,
                    'delete_requested_by': dr.requester.full_name if dr.requester else None,
                    'delete_request_reason': dr.reason,
                    'delete_approved_by': current_user.full_name,
                })
                db.session.add(AuditLog(
                    action='SAMPLE_DELETED',
                    entity_type='Sample',
                    entity_id=sample.id,
                    entity_label=sample.lab_number,
                    details=snapshot,
                    performed_by=current_user.id,
                    performed_at=now,
                ))
                # Remove files
                _delete_sample_files_main(sample)
                # Explicitly delete ReviewHistory records
                ReviewHistory.query.filter_by(sample_id=sample.id).delete(
                    synchronize_session=False
                )
                # Save sample_id for notification cleanup before nulling the FK
                sample_id_for_cleanup = sample.id
                # Null-out the FK on this delete request before deleting the sample
                dr.sample_id = None
                dr.assignment_id = None
                db.session.flush()
                db.session.delete(sample)
                # Remove related notifications using the numeric ID (avoids substring matching)
                Notification.query.filter(
                    Notification.link.like(f'%/samples/{sample_id_for_cleanup}%')
                ).delete(synchronize_session=False)

        elif dr.request_type == 'assignment' and dr.assignment_id:
            assignment = db.session.get(SampleAssignment, dr.assignment_id)
            if assignment:
                sample = assignment.sample
                chemist_name = assignment.chemist.full_name if assignment.chemist else 'Unknown'
                test_name = assignment.test_name
                chemist_id = assignment.chemist_id
                sample_ref = sample.lab_number
                # Audit the assignment deletion
                snapshot = _json.dumps({
                    'assignment_id': assignment.id,
                    'sample_lab_number': sample_ref,
                    'test_name': test_name,
                    'chemist_name': chemist_name,
                    'status': assignment.status.value,
                    'delete_request_id': dr.id,
                    'delete_requested_by': dr.requester.full_name if dr.requester else None,
                    'delete_request_reason': dr.reason,
                    'delete_approved_by': current_user.full_name,
                })
                db.session.add(AuditLog(
                    action='ASSIGNMENT_DELETED',
                    entity_type='SampleAssignment',
                    entity_id=assignment.id,
                    entity_label=dr.entity_label,
                    details=snapshot,
                    performed_by=current_user.id,
                    performed_at=now,
                ))
                # Log in sample history before deleting
                db.session.add(SampleHistory(
                    sample_id=sample.id,
                    action='Assignment Deleted',
                    details=(
                        f'{current_user.full_name} deleted assignment of test '
                        f'"{test_name}" from {chemist_name} '
                        f'(approved delete request by {dr.requester.full_name if dr.requester else "Unknown"}).'),
                    performed_by=current_user.id,
                    action_type='Assignment Deleted',
                    object_affected='Sample Assignment',
                    change_description=(
                        f'Test "{test_name}" removed from {chemist_name} '
                        f'by {current_user.full_name}'),
                ))
                # Null-out FK on this request so the cascade doesn't cascade-delete it
                dr.assignment_id = None
                db.session.flush()
                # Update sample status before deleting the assignment
                remaining = sample.assignments.filter(
                    SampleAssignment.id != assignment.id
                ).all()
                db.session.delete(assignment)
                db.session.flush()
                if not remaining:
                    sample.status = SampleStatus.REGISTERED
                # Notify the removed chemist
                from app.notifications import notify_assignment_removed
                notify_assignment_removed(
                    chemist_id, sample_ref, test_name, current_user.full_name, sample.id
                )

    # Log the decision in AuditLog regardless of outcome
    db.session.add(AuditLog(
        action=f'DELETE_REQUEST_{decision.upper()}',
        entity_type='DeleteRequest',
        entity_id=dr.id,
        entity_label=dr.entity_label,
        details=json.dumps({
            'request_type': dr.request_type,
            'entity': dr.entity_label,
            'requested_by': dr.requester.full_name if dr.requester else 'Unknown',
            'decision': decision,
            'comments': comments or None,
        }),
        performed_by=current_user.id,
        performed_at=now,
    ))

    db.session.commit()

    from app.notifications import notify_delete_request_decided
    notify_delete_request_decided(dr)
    db.session.commit()

    flash(f'Deletion request {decision}.', 'success')
    return redirect(url_for('main.delete_requests'))


def _delete_sample_files_main(sample):
    """Remove all uploaded files associated with a sample from disk (used in main routes)."""
    from flask import current_app as _app
    import os as _os
    upload_folder = _app.config.get('UPLOAD_FOLDER')
    if not upload_folder:
        return
    paths_to_remove = set()
    if sample.scanned_file:
        paths_to_remove.add(sample.scanned_file)
    if sample.summary_report_file:
        paths_to_remove.add(sample.summary_report_file)
    if sample.certificate_file:
        paths_to_remove.add(sample.certificate_file)
    for assignment in sample.assignments.all():
        if assignment.report_file:
            paths_to_remove.add(assignment.report_file)
    for doc in sample.supporting_documents.all():
        if doc.file_path:
            paths_to_remove.add(doc.file_path)
    for dv in sample.document_versions.all():
        if dv.file_path:
            paths_to_remove.add(dv.file_path)
    for filename in paths_to_remove:
        full_path = _os.path.join(upload_folder, filename)
        if _os.path.isfile(full_path):
            try:
                _os.remove(full_path)
            except OSError:
                _app.logger.warning('Could not remove file %s', full_path)


# ---------------------------------------------------------------------------
# Activity History PDF Export
# ---------------------------------------------------------------------------

@main_bp.route('/samples/<int:sample_id>/history/pdf')
@login_required
def export_history_pdf(sample_id):
    """Export sample activity history as a simple HTML-based printable page."""
    sample = db.get_or_404(Sample, sample_id)
    history = SampleHistory.query.filter_by(
        sample_id=sample_id
    ).order_by(SampleHistory.created_at.asc()).all()

    return render_template(
        'history_export.html',
        sample=sample,
        history=history,
        now=jamaica_now(),
    )


# ---------------------------------------------------------------------------
# Document Preview
# ---------------------------------------------------------------------------

@main_bp.route('/preview/<path:filename>')
@login_required
def preview_file(filename):
    """Serve a file for inline preview."""
    import os
    from werkzeug.security import safe_join
    upload_folder = current_app.config['UPLOAD_FOLDER']
    filepath = safe_join(upload_folder, filename)
    if filepath is None or not os.path.isfile(filepath):
        abort(404)

    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

    # Determine MIME type for inline preview
    mime_map = {
        'pdf': 'application/pdf',
        'png': 'image/png',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'gif': 'image/gif',
        'bmp': 'image/bmp',
        'tiff': 'image/tiff',
        'doc': 'application/msword',
        'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'xls': 'application/vnd.ms-excel',
        'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    }
    mime_type = mime_map.get(ext, 'application/octet-stream')

    from flask import send_from_directory
    return send_from_directory(
        upload_folder, filename,
        mimetype=mime_type,
        as_attachment=False,
    )


@main_bp.route('/preview-docx/<path:filename>')
@login_required
def preview_docx_as_pdf(filename):
    """Convert a DOC/DOCX file to PDF using LibreOffice and serve it inline.

    The converted PDF is cached in ``<UPLOAD_FOLDER>/pdf_cache/`` so that
    subsequent previews are served instantly without re-running LibreOffice.
    If LibreOffice is not installed the user is redirected to download the
    original file instead.
    """
    import os
    import shutil
    import subprocess
    from flask import send_from_directory
    from werkzeug.utils import secure_filename

    # Reject any path that would escape the uploads directory
    safe_name = secure_filename(filename)
    if not safe_name or safe_name != filename:
        abort(404)

    upload_folder = current_app.config['UPLOAD_FOLDER']
    filepath = os.path.join(upload_folder, safe_name)
    if not os.path.isfile(filepath):
        abort(404)

    ext = safe_name.rsplit('.', 1)[-1].lower() if '.' in safe_name else ''
    if ext not in ('doc', 'docx'):
        abort(400)

    # Cache directory lives inside the uploads folder so it shares the same
    # permissions and backup strategy as the originals.
    cache_dir = os.path.join(upload_folder, 'pdf_cache')
    os.makedirs(cache_dir, exist_ok=True)

    base_name = os.path.splitext(safe_name)[0]
    cached_pdf_name = base_name + '.pdf'
    cached_pdf_path = os.path.join(cache_dir, cached_pdf_name)

    if not os.path.isfile(cached_pdf_path):
        lo_cmd = shutil.which('libreoffice') or shutil.which('soffice')
        if not lo_cmd:
            current_app.logger.warning(
                'LibreOffice not found; cannot convert %s to PDF', safe_name
            )
            flash(
                'Document preview is not available on this server. '
                'Please download the file to view it.',
                'warning',
            )
            return redirect(url_for('samples.download_file', filename=safe_name))

        try:
            subprocess.run(
                [
                    lo_cmd,
                    '--headless',
                    '--convert-to', 'pdf',
                    '--outdir', cache_dir,
                    filepath,
                ],
                check=True,
                timeout=30,
                capture_output=True,
            )
        except subprocess.TimeoutExpired:
            current_app.logger.error(
                'LibreOffice conversion timed out for %s', safe_name
            )
            flash('Document conversion timed out. Please download the file instead.', 'warning')
            return redirect(url_for('samples.download_file', filename=safe_name))
        except subprocess.CalledProcessError as exc:
            current_app.logger.error(
                'LibreOffice conversion failed for %s: %s', safe_name, exc.stderr
            )
            flash('Document conversion failed. Please download the file instead.', 'warning')
            return redirect(url_for('samples.download_file', filename=safe_name))

    if not os.path.isfile(cached_pdf_path):
        flash('Document conversion produced no output. Please download the file instead.', 'warning')
        return redirect(url_for('samples.download_file', filename=safe_name))

    return send_from_directory(
        cache_dir,
        cached_pdf_name,
        mimetype='application/pdf',
        as_attachment=False,
    )


# ---------------------------------------------------------------------------
# Data Export / Import  (Admin only)
# ---------------------------------------------------------------------------

def _serialize_value(val):
    """Convert a Python value to a JSON-safe representation."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, enum.Enum):
        return val.value
    return val


def _table_to_dicts(model_class):
    """Serialize all rows of a SQLAlchemy model to a list of dicts."""
    rows = []
    mapper = db.inspect(model_class)
    columns = [c.key for c in mapper.columns]
    for obj in model_class.query.all():
        row = {}
        for col in columns:
            row[col] = _serialize_value(getattr(obj, col))
        rows.append(row)
    return rows


def _assoc_table_to_dicts(table):
    """Serialize an association table to a list of dicts."""
    rows = []
    result = db.session.execute(table.select()).fetchall()
    col_names = [c.name for c in table.columns]
    for r in result:
        row = {}
        for i, name in enumerate(col_names):
            row[name] = _serialize_value(r[i])
        rows.append(row)
    return rows


@main_bp.route('/export-data')
@login_required
def export_data():
    """Export all application data as a ZIP file (JSON + uploaded files).
    Admin-only."""
    if not current_user.has_role(Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    import json
    import zipfile
    import os

    # Build the JSON payload with all tables
    data = {
        'export_version': 2,
        'exported_at': jamaica_now().isoformat(),
        'tables': {
            'users': _table_to_dicts(User),
            'user_roles': _assoc_table_to_dicts(user_roles),
            'user_branches': _assoc_table_to_dicts(user_branches),
            'user_permissions': _assoc_table_to_dicts(user_permissions),
            'custom_roles': _table_to_dicts(CustomRole),
            'custom_role_permissions': _assoc_table_to_dicts(custom_role_permissions),
            'user_custom_roles': _assoc_table_to_dicts(user_custom_roles),
            'settings': _table_to_dicts(Setting),
            'samples': _table_to_dicts(Sample),
            'sample_assignments': _table_to_dicts(SampleAssignment),
            'sample_history': _table_to_dicts(SampleHistory),
            'review_history': _table_to_dicts(ReviewHistory),
            'notifications': _table_to_dicts(Notification),
            'kpi_targets': _table_to_dicts(KpiTarget),
            'non_working_days': _table_to_dicts(NonWorkingDay),
            'supporting_documents': _table_to_dicts(SupportingDocument),
            'document_versions': _table_to_dicts(DocumentVersion),
            'back_date_requests': _table_to_dicts(BackDateRequest),
            'delete_requests': _table_to_dicts(DeleteRequest),
            'audit_log': _table_to_dicts(AuditLog),
            'direct_messages': _table_to_dicts(DirectMessage),
            'acting_roles': _table_to_dicts(ActingRole),
        },
    }

    # Counts for quick verification
    data['row_counts'] = {k: len(v) for k, v in data['tables'].items()}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('data.json', json.dumps(data, indent=2, default=str))

        # Bundle uploaded files
        upload_folder = current_app.config.get('UPLOAD_FOLDER', '')
        if upload_folder and os.path.isdir(upload_folder):
            for root, _dirs, files in os.walk(upload_folder):
                for fname in files:
                    full_path = os.path.join(root, fname)
                    arc_name = os.path.relpath(full_path, upload_folder)
                    zf.write(full_path, f'uploads/{arc_name}')

    buf.seek(0)
    timestamp = jamaica_now().strftime('%Y%m%d_%H%M%S')
    return send_file(
        buf,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'dgc_sms_export_{timestamp}.zip',
    )


def _parse_date(val):
    """Parse an ISO date string to a date object, or return None."""
    if not val:
        return None
    try:
        return date.fromisoformat(val)
    except (ValueError, TypeError):
        return None


def _parse_datetime(val):
    """Parse an ISO datetime string to a datetime object, or return None."""
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except (ValueError, TypeError):
        return None


def _parse_enum(val, enum_class):
    """Convert a string value to the corresponding enum member, or None."""
    if val is None:
        return None
    for member in enum_class:
        if member.value == val:
            return member
    return None


# Column type hints for correct deserialization
_DATE_COLUMNS = {
    'date_received', 'expected_report_date', 'expiration_date',
    'expected_completion', 'test_date', 'date',
}
_DATETIME_COLUMNS = {
    'created_at', 'date_registered', 'summary_report_at',
    'deputy_reviewed_at', 'certificate_prepared_at',
    'hod_reviewed_at', 'certified_at', 'assigned_date',
    'date_completed', 'report_submitted_at',
    'preliminary_reviewed_at', 'reviewed_at', 'uploaded_at',
    'requested_at', 'decided_at',
    'performed_at', 'locked_until', 'last_seen',
}


def _coerce_row(table_name, row):
    """Coerce string values back to proper Python types for a given table."""
    import copy
    row = copy.copy(row)

    # Enum columns per table
    enum_map = {
        'users': {'role': Role, 'branch': Branch},
        'user_roles': {'role': Role},
        'user_branches': {'branch': Branch},
        'user_permissions': {'permission': Permission},
        'custom_role_permissions': {'permission': Permission},
        'samples': {
            'sample_type': Branch,
            'status': SampleStatus,
        },
        'sample_assignments': {'status': AssignmentStatus},
    }

    enums = enum_map.get(table_name, {})
    for col, val in list(row.items()):
        if col in enums:
            row[col] = _parse_enum(val, enums[col])
        elif col in _DATE_COLUMNS:
            row[col] = _parse_date(val)
        elif col in _DATETIME_COLUMNS:
            row[col] = _parse_datetime(val)
        elif isinstance(val, str) and val == '':
            # Keep empty strings as-is for text columns
            pass

    # Boolean columns
    bool_cols = {
        'is_active_user', 'must_change_password', 'is_read',
        'email_sent', 'out_of_spec',
    }
    for col in bool_cols:
        if col in row and row[col] is not None:
            row[col] = bool(row[col])

    return row


@main_bp.route('/import-data', methods=['GET', 'POST'])
@login_required
def import_data():
    """Import application data from a previously exported ZIP file.
    Admin-only. This REPLACES all data in the database."""
    if not current_user.has_role(Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    if request.method == 'GET':
        return redirect(url_for('main.settings'))

    import json
    import zipfile
    import os
    import shutil

    f = request.files.get('import_file')
    if not f or not f.filename:
        flash('No file selected.', 'warning')
        return redirect(url_for('main.settings'))

    if not f.filename.lower().endswith('.zip'):
        flash('Please upload a .zip export file.', 'danger')
        return redirect(url_for('main.settings'))

    try:
        zf = zipfile.ZipFile(f.stream)
    except zipfile.BadZipFile:
        flash('Invalid ZIP file.', 'danger')
        return redirect(url_for('main.settings'))

    if 'data.json' not in zf.namelist():
        flash('Invalid export file — missing data.json.', 'danger')
        return redirect(url_for('main.settings'))

    try:
        raw = zf.read('data.json')
        data = json.loads(raw)
    except (json.JSONDecodeError, KeyError):
        flash('Corrupt data.json in export file.', 'danger')
        return redirect(url_for('main.settings'))

    if 'tables' not in data:
        flash('Invalid export format — missing tables key.', 'danger')
        return redirect(url_for('main.settings'))

    tables = data['tables']

    # --- Wipe existing data in reverse dependency order ---
    # Disable FK checks for the duration of the import
    try:
        # Delete in FK-safe order (children first)
        AuditLog.query.delete()
        BackDateRequest.query.delete()
        DeleteRequest.query.delete()
        DirectMessage.query.delete()
        DocumentVersion.query.delete()
        SupportingDocument.query.delete()
        ReviewHistory.query.delete()
        Notification.query.delete()
        SampleHistory.query.delete()
        SampleAssignment.query.delete()
        Sample.query.delete()
        KpiTarget.query.delete()
        NonWorkingDay.query.delete()
        Setting.query.delete()
        db.session.execute(user_roles.delete())
        db.session.execute(user_branches.delete())
        db.session.execute(user_permissions.delete())
        db.session.execute(custom_role_permissions.delete())
        db.session.execute(user_custom_roles.delete())
        CustomRole.query.delete()
        User.query.delete()
        db.session.flush()

        # --- Insert in FK-safe order (parents first) ---

        # 1. Users (without roles/branches association — those come next)
        for row in tables.get('users', []):
            row = _coerce_row('users', row)
            user = User(
                id=row.get('id'),
                email=row['email'],
                username=row['username'],
                first_name=row['first_name'],
                last_name=row['last_name'],
                password_hash=row['password_hash'],
                role=row.get('role'),
                branch=row.get('branch'),
                is_active_user=row.get('is_active_user', True),
                must_change_password=row.get('must_change_password', False),
                created_at=row.get('created_at'),
                failed_login_attempts=row.get('failed_login_attempts', 0),
                locked_until=row.get('locked_until'),
                last_seen=row.get('last_seen'),
            )
            db.session.add(user)
        db.session.flush()

        # 2. User roles & branches
        for row in tables.get('user_roles', []):
            row = _coerce_row('user_roles', row)
            if row.get('role') is not None:
                db.session.execute(user_roles.insert().values(
                    user_id=row['user_id'], role=row['role']
                ))

        for row in tables.get('user_branches', []):
            row = _coerce_row('user_branches', row)
            if row.get('branch') is not None:
                db.session.execute(user_branches.insert().values(
                    user_id=row['user_id'], branch=row['branch']
                ))

        for row in tables.get('user_permissions', []):
            row = _coerce_row('user_permissions', row)
            if row.get('permission') is not None:
                db.session.execute(user_permissions.insert().values(
                    user_id=row['user_id'], permission=row['permission']
                ))

        for row in tables.get('custom_roles', []):
            cr = CustomRole(
                id=row.get('id'),
                name=row['name'],
                description=row.get('description'),
                created_at=_parse_datetime(row.get('created_at')),
            )
            db.session.add(cr)
        db.session.flush()

        for row in tables.get('custom_role_permissions', []):
            row = _coerce_row('custom_role_permissions', row)
            if row.get('permission') is not None:
                db.session.execute(custom_role_permissions.insert().values(
                    custom_role_id=row['custom_role_id'],
                    permission=row['permission'],
                ))

        for row in tables.get('user_custom_roles', []):
            db.session.execute(user_custom_roles.insert().values(
                user_id=row['user_id'],
                custom_role_id=row['custom_role_id'],
            ))
        db.session.flush()

        # 3. Settings
        for row in tables.get('settings', []):
            db.session.add(Setting(key=row['key'], value=row.get('value', '')))
        db.session.flush()

        # 4. Samples
        for row in tables.get('samples', []):
            row = _coerce_row('samples', row)
            s = Sample()
            for col, val in row.items():
                if hasattr(s, col):
                    setattr(s, col, val)
            db.session.add(s)
        db.session.flush()

        # 5. Sample Assignments
        for row in tables.get('sample_assignments', []):
            row = _coerce_row('sample_assignments', row)
            sa = SampleAssignment()
            for col, val in row.items():
                if hasattr(sa, col):
                    setattr(sa, col, val)
            db.session.add(sa)
        db.session.flush()

        # 6. Sample History
        for row in tables.get('sample_history', []):
            row = _coerce_row('sample_history', row)
            sh = SampleHistory()
            for col, val in row.items():
                if hasattr(sh, col):
                    setattr(sh, col, val)
            db.session.add(sh)

        # 7. Review History
        for row in tables.get('review_history', []):
            row = _coerce_row('review_history', row)
            rh = ReviewHistory()
            for col, val in row.items():
                if hasattr(rh, col):
                    setattr(rh, col, val)
            db.session.add(rh)

        # 8. Notifications
        for row in tables.get('notifications', []):
            row = _coerce_row('notifications', row)
            n = Notification()
            for col, val in row.items():
                if hasattr(n, col):
                    setattr(n, col, val)
            db.session.add(n)

        # 9. KPI Targets
        for row in tables.get('kpi_targets', []):
            row = _coerce_row('kpi_targets', row)
            kt = KpiTarget()
            for col, val in row.items():
                if hasattr(kt, col):
                    setattr(kt, col, val)
            db.session.add(kt)

        # 10. Non-Working Days
        for row in tables.get('non_working_days', []):
            row = _coerce_row('non_working_days', row)
            nwd = NonWorkingDay()
            for col, val in row.items():
                if hasattr(nwd, col):
                    setattr(nwd, col, val)
            db.session.add(nwd)

        # 11. Supporting Documents
        for row in tables.get('supporting_documents', []):
            row = _coerce_row('supporting_documents', row)
            sd = SupportingDocument()
            for col, val in row.items():
                if hasattr(sd, col):
                    setattr(sd, col, val)
            db.session.add(sd)

        # 12. Document Versions
        for row in tables.get('document_versions', []):
            row = _coerce_row('document_versions', row)
            dv = DocumentVersion()
            for col, val in row.items():
                if hasattr(dv, col):
                    setattr(dv, col, val)
            db.session.add(dv)

        # 13. Back-Date Requests
        for row in tables.get('back_date_requests', []):
            row = _coerce_row('back_date_requests', row)
            bdr = BackDateRequest()
            for col, val in row.items():
                if hasattr(bdr, col):
                    setattr(bdr, col, val)
            db.session.add(bdr)

        # 14. Audit Log
        for row in tables.get('audit_log', []):
            row = _coerce_row('audit_log', row)
            al = AuditLog()
            for col, val in row.items():
                if hasattr(al, col):
                    setattr(al, col, val)
            db.session.add(al)

        # 15. Delete Requests
        for row in tables.get('delete_requests', []):
            row = _coerce_row('delete_requests', row)
            dr = DeleteRequest()
            for col, val in row.items():
                if hasattr(dr, col):
                    setattr(dr, col, val)
            db.session.add(dr)

        # 16. Direct Messages
        for row in tables.get('direct_messages', []):
            row = _coerce_row('direct_messages', row)
            dm = DirectMessage()
            for col, val in row.items():
                if hasattr(dm, col):
                    setattr(dm, col, val)
            db.session.add(dm)

        db.session.commit()

        # --- Restore uploaded files ---
        upload_folder = current_app.config.get('UPLOAD_FOLDER', '')
        if upload_folder:
            # Clear existing uploads
            if os.path.isdir(upload_folder):
                for entry in os.listdir(upload_folder):
                    path = os.path.join(upload_folder, entry)
                    if os.path.isfile(path):
                        os.remove(path)
                    elif os.path.isdir(path):
                        shutil.rmtree(path)

            # Extract uploaded files from ZIP
            for name in zf.namelist():
                if name.startswith('uploads/') and not name.endswith('/'):
                    rel_path = name[len('uploads/'):]
                    # Sanitize: prevent directory traversal attacks
                    if '..' in rel_path or rel_path.startswith('/'):
                        continue
                    from werkzeug.utils import secure_filename
                    # Secure each path component individually
                    parts = rel_path.replace('\\', '/').split('/')
                    safe_parts = [secure_filename(p) for p in parts]
                    safe_parts = [p for p in safe_parts if p]  # drop empty
                    if not safe_parts:
                        continue
                    safe_rel = os.path.join(*safe_parts)
                    safe_dest = os.path.join(upload_folder, safe_rel)
                    # Final check: resolved path must be inside upload_folder
                    real_dest = os.path.realpath(safe_dest)
                    real_upload = os.path.realpath(upload_folder)
                    if not real_dest.startswith(real_upload + os.sep):
                        continue
                    os.makedirs(os.path.dirname(safe_dest), exist_ok=True)
                    with zf.open(name) as src, open(safe_dest, 'wb') as dst:
                        dst.write(src.read())

        zf.close()

        row_counts = data.get('row_counts', {})
        total = sum(row_counts.values()) if row_counts else '?'
        flash(f'Data imported successfully — {total} records restored.', 'success')

    except Exception as e:
        db.session.rollback()
        current_app.logger.exception('Data import failed')
        flash(f'Import failed: {e}', 'danger')

    return redirect(url_for('main.settings'))


# ---------------------------------------------------------------------------
# In-App Messenger
# ---------------------------------------------------------------------------

@main_bp.route('/messages')
@login_required
def messages_inbox():
    """Show all conversations for the current user."""
    from sqlalchemy import func, or_, and_
    uid = current_user.id

    # All users that exchanged at least one message with the current user
    sent_to = db.session.query(DirectMessage.recipient_id.label('other_id')).filter(
        DirectMessage.sender_id == uid
    )
    received_from = db.session.query(DirectMessage.sender_id.label('other_id')).filter(
        DirectMessage.recipient_id == uid
    )
    partner_ids = {row.other_id for row in sent_to.union(received_from).all()}

    conversations = []
    for pid in partner_ids:
        partner = db.session.get(User, pid)
        if not partner:
            continue
        # Most recent message in this conversation
        last_msg = DirectMessage.query.filter(
            or_(
                and_(DirectMessage.sender_id == uid, DirectMessage.recipient_id == pid),
                and_(DirectMessage.sender_id == pid, DirectMessage.recipient_id == uid),
            )
        ).order_by(DirectMessage.created_at.desc()).first()
        unread_count = DirectMessage.query.filter_by(
            sender_id=pid, recipient_id=uid, is_read=False
        ).count()
        conversations.append({
            'partner': partner,
            'last_msg': last_msg,
            'unread': unread_count,
        })

    # Sort by most recent message first
    conversations.sort(key=lambda c: c['last_msg'].created_at, reverse=True)

    # Users available to start a new conversation (all active users except self)
    all_users = User.query.filter(
        User.id != uid,
        User.is_active_user.is_(True),
    ).order_by(User.first_name, User.last_name).all()

    return render_template(
        'messages/inbox.html',
        conversations=conversations,
        all_users=all_users,
    )


@main_bp.route('/messages/<int:partner_id>', methods=['GET', 'POST'])
@login_required
def messages_conversation(partner_id):
    """View and send messages in a conversation with partner_id."""
    from sqlalchemy import or_, and_

    partner = db.get_or_404(User, partner_id)
    if partner.id == current_user.id:
        flash('You cannot message yourself.', 'warning')
        return redirect(url_for('main.messages_inbox'))

    uid = current_user.id

    if request.method == 'POST':
        body = request.form.get('body', '').strip()
        if not body:
            flash('Message cannot be empty.', 'warning')
            return redirect(url_for('main.messages_conversation', partner_id=partner_id))
        if len(body) > 4000:
            flash('Message is too long (max 4000 characters).', 'warning')
            return redirect(url_for('main.messages_conversation', partner_id=partner_id))
        msg = DirectMessage(sender_id=uid, recipient_id=partner_id, body=body)
        db.session.add(msg)
        db.session.commit()
        return redirect(url_for('main.messages_conversation', partner_id=partner_id))

    # Mark all incoming messages from partner as read
    DirectMessage.query.filter_by(
        sender_id=partner_id, recipient_id=uid, is_read=False
    ).update({'is_read': True})
    db.session.commit()

    # Load full thread ordered oldest→newest
    thread = DirectMessage.query.filter(
        or_(
            and_(DirectMessage.sender_id == uid, DirectMessage.recipient_id == partner_id),
            and_(DirectMessage.sender_id == partner_id, DirectMessage.recipient_id == uid),
        )
    ).order_by(DirectMessage.created_at.asc()).all()

    # Users available to start a new conversation (for sidebar)
    all_users = User.query.filter(
        User.id != uid,
        User.is_active_user.is_(True),
    ).order_by(User.first_name, User.last_name).all()

    return render_template(
        'messages/conversation.html',
        partner=partner,
        thread=thread,
        all_users=all_users,
    )


@main_bp.route('/api/messages/unread-count')
@login_required
def unread_message_count():
    count = DirectMessage.query.filter_by(
        recipient_id=current_user.id, is_read=False
    ).count()
    return jsonify({'count': count})


# ---------------------------------------------------------------------------
# Dropdown Configuration Admin  (Feature 11)
# ---------------------------------------------------------------------------

@main_bp.route('/admin/dropdowns')
@login_required
def admin_dropdowns():
    """List all dropdown configuration entries."""
    if not (current_user.has_any_role(Role.ADMIN, Role.HOD)
            or current_user.has_permission(Permission.MANAGE_DROPDOWNS)):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    from app.forms import DropdownConfigForm, DropdownBulkAddForm, DROPDOWN_CATEGORY_CHOICES
    category_filter = request.args.get('category', '')
    page = request.args.get('page', 1, type=int)
    q = DropdownConfig.query
    if category_filter:
        q = q.filter_by(category=category_filter)
    pagination = q.order_by(
        DropdownConfig.category, db.func.lower(DropdownConfig.label), DropdownConfig.label
    ).paginate(page=page, per_page=25, error_out=False)
    # All items (unfiltered) used by the JS category preview in the add form
    all_items = DropdownConfig.query.order_by(
        DropdownConfig.category, db.func.lower(DropdownConfig.label), DropdownConfig.label
    ).all()
    form = DropdownConfigForm()
    bulk_form = DropdownBulkAddForm()
    return render_template(
        'admin/dropdowns.html',
        items=pagination.items, form=form, bulk_form=bulk_form,
        category_filter=category_filter,
        category_choices=DROPDOWN_CATEGORY_CHOICES,
        all_items=all_items,
        pagination=pagination,
    )


@main_bp.route('/admin/dropdowns/add', methods=['POST'])
@login_required
def admin_dropdown_add():
    """Add a new dropdown configuration entry."""
    if not (current_user.has_any_role(Role.ADMIN, Role.HOD)
            or current_user.has_permission(Permission.MANAGE_DROPDOWNS)):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    from app.forms import DropdownConfigForm
    form = DropdownConfigForm()
    if form.validate_on_submit():
        branch_val = form.branch.data or None
        existing = DropdownConfig.query.filter_by(
            category=form.category.data, value=form.value.data, branch=branch_val
        ).first()
        if existing:
            flash(f'Entry "{form.value.data}" already exists in category "{form.category.data}".', 'warning')
        else:
            db.session.add(DropdownConfig(
                category=form.category.data,
                value=form.value.data,
                label=form.label.data or form.value.data,
                branch=branch_val,
                sort_order=form.sort_order.data or 0,
                is_active=form.is_active.data,
                created_by=current_user.id,
            ))
            db.session.commit()
            flash('Dropdown entry added.', 'success')
    else:
        for field, errs in form.errors.items():
            for err in errs:
                flash(f'{field}: {err}', 'danger')
    return redirect(url_for('main.admin_dropdowns'))


@main_bp.route('/admin/dropdowns/bulk_add', methods=['POST'])
@login_required
def admin_dropdown_bulk_add():
    """Bulk-add multiple dropdown entries for a single category."""
    if not (current_user.has_any_role(Role.ADMIN, Role.HOD)
            or current_user.has_permission(Permission.MANAGE_DROPDOWNS)):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    from app.forms import DropdownBulkAddForm
    from sqlalchemy.exc import IntegrityError
    form = DropdownBulkAddForm()
    if form.validate_on_submit():
        category = form.category.data
        branch_val = form.branch.data or None
        is_active = form.is_active.data
        lines = [l.strip() for l in form.bulk_values.data.splitlines() if l.strip()]
        added = 0
        skipped = 0
        seen_values = set()
        for line in lines:
            if '|' in line:
                value, _, label = line.partition('|')
                value = value.strip()
                label = label.strip() or value
            else:
                value = line
                label = line
            if not value:
                continue
            # Skip duplicates within the same batch
            value_key = value.lower()
            if value_key in seen_values:
                skipped += 1
                continue
            seen_values.add(value_key)
            existing = DropdownConfig.query.filter_by(
                category=category, value=value, branch=branch_val
            ).first()
            if existing:
                skipped += 1
            else:
                db.session.add(DropdownConfig(
                    category=category,
                    value=value,
                    label=label,
                    branch=branch_val,
                    sort_order=0,
                    is_active=is_active,
                    created_by=current_user.id,
                ))
                added += 1
        if added:
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash('Unable to save entries due to duplicate values. Please try again.', 'danger')
                return redirect(url_for('main.admin_dropdowns'))
        parts = []
        if added:
            parts.append(f'{added} entr{"y" if added == 1 else "ies"} added')
        if skipped:
            parts.append(f'{skipped} duplicate{"s" if skipped > 1 else ""} skipped')
        if parts:
            flash(', '.join(parts).capitalize() + '.', 'success' if added else 'warning')
    else:
        for field, errs in form.errors.items():
            for err in errs:
                flash(f'{field}: {err}', 'danger')
    return redirect(url_for('main.admin_dropdowns'))


@main_bp.route('/admin/dropdowns/<int:item_id>/edit', methods=['GET', 'POST'])
@login_required
def admin_dropdown_edit(item_id):
    """Edit a dropdown configuration entry."""
    if not (current_user.has_any_role(Role.ADMIN, Role.HOD)
            or current_user.has_permission(Permission.MANAGE_DROPDOWNS)):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    from app.forms import DropdownConfigForm
    item = db.get_or_404(DropdownConfig, item_id)
    form = DropdownConfigForm(obj=item)
    if form.validate_on_submit():
        item.category = form.category.data
        item.value = form.value.data
        item.label = form.label.data or form.value.data
        item.branch = form.branch.data or None
        item.sort_order = form.sort_order.data or 0
        item.is_active = form.is_active.data
        db.session.commit()
        flash('Dropdown entry updated.', 'success')
        return redirect(url_for('main.admin_dropdowns'))
    return render_template('admin/dropdown_edit.html', form=form, item=item)


@main_bp.route('/admin/dropdowns/<int:item_id>/delete', methods=['POST'])
@login_required
def admin_dropdown_delete(item_id):
    """Delete a dropdown configuration entry."""
    if not (current_user.has_any_role(Role.ADMIN, Role.HOD)
            or current_user.has_permission(Permission.MANAGE_DROPDOWNS)):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    item = db.get_or_404(DropdownConfig, item_id)
    db.session.delete(item)
    db.session.commit()
    flash('Dropdown entry deleted.', 'success')
    return redirect(url_for('main.admin_dropdowns'))


# ---------------------------------------------------------------------------
# KPI – Month-level aggregation  (Feature 3)
# ---------------------------------------------------------------------------

@main_bp.route('/kpi/monthly')
@login_required
def kpi_monthly():
    """Monthly KPI summary — all labs selectable."""
    if not (current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                      Role.DEPUTY, Role.ADMIN)
            or current_user.has_permission(Permission.KPI_VIEW)):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    # Lab groups: key -> (display label, icon, [Branch enum members])
    LAB_GROUPS = {
        'pharmaceutical': (
            'Pharmaceutical', 'bi-capsule',
            [Branch.PHARMACEUTICAL, Branch.PHARMACEUTICAL_NR],
        ),
        'toxicology': (
            'Toxicology', 'bi-droplet',
            [Branch.TOXICOLOGY],
        ),
        'milk': (
            'Milk (Food)', 'bi-cup-straw',
            [Branch.FOOD_MILK],
        ),
        'alcohol': (
            'Alcohol (Food)', 'bi-cup',
            [Branch.FOOD_ALCOHOL],
        ),
    }

    lab_key = request.args.get('lab', 'pharmaceutical')
    if lab_key not in LAB_GROUPS:
        lab_key = 'pharmaceutical'
    lab_label, lab_icon, lab_branches = LAB_GROUPS[lab_key]

    year = request.args.get('year', type=int, default=_current_fiscal_year())
    available_years = _available_fiscal_years()

    month_names = {
        1: 'Jan', 2: 'Feb', 3: 'Mar', 4: 'Apr', 5: 'May', 6: 'Jun',
        7: 'Jul', 8: 'Aug', 9: 'Sep', 10: 'Oct', 11: 'Nov', 12: 'Dec',
    }
    # Fiscal year spans April of `year` to March of `year+1`
    fiscal_months = [(year, m) for m in range(4, 13)] + [(year + 1, m) for m in range(1, 4)]

    months_data = []
    for cal_year, cal_month in fiscal_months:
        base = Sample.query.filter(
            Sample.sample_type.in_(lab_branches),
            db.extract('year', Sample.date_registered) == cal_year,
            db.extract('month', Sample.date_registered) == cal_month,
        )
        total = base.count()
        certified = base.filter(
            Sample.status.in_([SampleStatus.CERTIFIED, SampleStatus.COMPLETED])
        ).count()
        sample_ids = [s.id for s in base.all()]
        tests_performed = SampleAssignment.query.filter(
            SampleAssignment.sample_id.in_(sample_ids),
            SampleAssignment.status.in_([
                AssignmentStatus.ACCEPTED, AssignmentStatus.COMPLETED,
                AssignmentStatus.REPORT_SUBMITTED,
                AssignmentStatus.UNDER_PRELIMINARY_REVIEW,
                AssignmentStatus.UNDER_TECHNICAL_REVIEW,
            ]),
        ).count() if sample_ids else 0
        months_data.append({
            'year': cal_year,
            'month': cal_month,
            'month_name': month_names[cal_month],
            'total': total,
            'certified': certified,
            'tests_performed': tests_performed,
        })

    return render_template(
        'kpi_monthly.html',
        months_data=months_data,
        year=year,
        available_years=available_years,
        lab_key=lab_key,
        lab_label=lab_label,
        lab_icon=lab_icon,
        lab_groups=LAB_GROUPS,
    )


# ---------------------------------------------------------------------------
# Audit Log view
# ---------------------------------------------------------------------------

@main_bp.route('/audit-log')
@login_required
def audit_log():
    """View the permanent audit log – Admin, SuperAdmin, HOD, or users with AUDIT_LOG_VIEW."""
    if not (current_user.has_any_role(Role.ADMIN, Role.SUPER_ADMIN, Role.HOD)
            or current_user.has_permission(Permission.AUDIT_LOG_VIEW)):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    page = request.args.get('page', 1, type=int)
    q_action = request.args.get('action', '').strip()
    q_entity = request.args.get('entity', '').strip()
    q_user = request.args.get('user', '').strip()
    q_date_from = request.args.get('date_from', '').strip()
    q_date_to = request.args.get('date_to', '').strip()
    q_role = request.args.get('role', '').strip()
    q_report_type = request.args.get('report_type', '').strip()
    q_action_type = request.args.get('action_type', '').strip()
    q_stage = request.args.get('stage', '').strip()
    q_keyword = request.args.get('keyword', '').strip()

    query = AuditLog.query
    if q_action:
        query = query.filter(AuditLog.action.ilike(f'%{q_action}%'))
    if q_entity:
        query = query.filter(
            db.or_(
                AuditLog.entity_type.ilike(f'%{q_entity}%'),
                AuditLog.entity_label.ilike(f'%{q_entity}%'),
            )
        )
    if q_user:
        matching_users = User.query.filter(
            db.or_(
                User.first_name.ilike(f'%{q_user}%'),
                User.last_name.ilike(f'%{q_user}%'),
                User.username.ilike(f'%{q_user}%'),
            )
        ).with_entities(User.id).all()
        user_ids = [u.id for u in matching_users]
        query = query.filter(AuditLog.performed_by.in_(user_ids))
    if q_date_from:
        try:
            date_from = datetime.strptime(q_date_from, '%Y-%m-%d')
            query = query.filter(AuditLog.performed_at >= date_from)
        except ValueError:
            pass
    if q_date_to:
        try:
            date_to = datetime.strptime(q_date_to, '%Y-%m-%d').replace(
                hour=23, minute=59, second=59)
            query = query.filter(AuditLog.performed_at <= date_to)
        except ValueError:
            pass
    if q_role:
        query = query.filter(AuditLog.user_role.ilike(f'%{q_role}%'))
    if q_report_type:
        query = query.filter(AuditLog.report_type.ilike(f'%{q_report_type}%'))
    if q_action_type:
        query = query.filter(AuditLog.action.ilike(f'%{q_action_type}%'))
    if q_stage:
        query = query.filter(
            db.or_(
                AuditLog.previous_stage.ilike(f'%{q_stage}%'),
                AuditLog.new_stage.ilike(f'%{q_stage}%'),
            )
        )
    if q_keyword:
        query = query.filter(
            db.or_(
                AuditLog.human_description.ilike(f'%{q_keyword}%'),
                AuditLog.entity_label.ilike(f'%{q_keyword}%'),
                AuditLog.details.ilike(f'%{q_keyword}%'),
                AuditLog.comments.ilike(f'%{q_keyword}%'),
            )
        )

    pagination = query.order_by(AuditLog.performed_at.desc()).paginate(
        page=page, per_page=50, error_out=False
    )

    # Provide filter option lists
    roles_list = [r.value for r in Role]
    report_types_list = [b.value for b in Branch]
    stages_list = [s.value for s in SampleStatus]

    return render_template(
        'audit_log.html',
        entries=pagination.items,
        pagination=pagination,
        q_action=q_action,
        q_entity=q_entity,
        q_user=q_user,
        q_date_from=q_date_from,
        q_date_to=q_date_to,
        q_role=q_role,
        q_report_type=q_report_type,
        q_action_type=q_action_type,
        q_stage=q_stage,
        q_keyword=q_keyword,
        roles_list=roles_list,
        report_types_list=report_types_list,
        stages_list=stages_list,
    )

# ---------------------------------------------------------------------------
# Audit Log PDF Export
# ---------------------------------------------------------------------------

@main_bp.route('/audit-log/export/pdf')
@login_required
def audit_log_export_pdf():
    """Export filtered audit log entries to PDF."""
    if not (current_user.has_any_role(Role.ADMIN, Role.SUPER_ADMIN, Role.HOD)
            or current_user.has_permission(Permission.AUDIT_LOG_EXPORT)):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    import io
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    )
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    # Apply same filters as audit_log view
    q_action = request.args.get('action', '').strip()
    q_entity = request.args.get('entity', '').strip()
    q_user = request.args.get('user', '').strip()
    q_date_from = request.args.get('date_from', '').strip()
    q_date_to = request.args.get('date_to', '').strip()
    q_role = request.args.get('role', '').strip()
    q_report_type = request.args.get('report_type', '').strip()
    q_action_type = request.args.get('action_type', '').strip()
    q_stage = request.args.get('stage', '').strip()
    q_keyword = request.args.get('keyword', '').strip()

    query = AuditLog.query
    if q_action:
        query = query.filter(AuditLog.action.ilike(f'%{q_action}%'))
    if q_entity:
        query = query.filter(
            db.or_(
                AuditLog.entity_type.ilike(f'%{q_entity}%'),
                AuditLog.entity_label.ilike(f'%{q_entity}%'),
            )
        )
    if q_user:
        matching_users = User.query.filter(
            db.or_(
                User.first_name.ilike(f'%{q_user}%'),
                User.last_name.ilike(f'%{q_user}%'),
                User.username.ilike(f'%{q_user}%'),
            )
        ).with_entities(User.id).all()
        user_ids = [u.id for u in matching_users]
        query = query.filter(AuditLog.performed_by.in_(user_ids))
    if q_date_from:
        try:
            date_from = datetime.strptime(q_date_from, '%Y-%m-%d')
            query = query.filter(AuditLog.performed_at >= date_from)
        except ValueError:
            pass
    if q_date_to:
        try:
            date_to = datetime.strptime(q_date_to, '%Y-%m-%d').replace(
                hour=23, minute=59, second=59)
            query = query.filter(AuditLog.performed_at <= date_to)
        except ValueError:
            pass
    if q_role:
        query = query.filter(AuditLog.user_role.ilike(f'%{q_role}%'))
    if q_report_type:
        query = query.filter(AuditLog.report_type.ilike(f'%{q_report_type}%'))
    if q_action_type:
        query = query.filter(AuditLog.action.ilike(f'%{q_action_type}%'))
    if q_stage:
        query = query.filter(
            db.or_(
                AuditLog.previous_stage.ilike(f'%{q_stage}%'),
                AuditLog.new_stage.ilike(f'%{q_stage}%'),
            )
        )
    if q_keyword:
        query = query.filter(
            db.or_(
                AuditLog.human_description.ilike(f'%{q_keyword}%'),
                AuditLog.entity_label.ilike(f'%{q_keyword}%'),
                AuditLog.details.ilike(f'%{q_keyword}%'),
                AuditLog.comments.ilike(f'%{q_keyword}%'),
            )
        )

    # Limit to 5000 records
    entries = query.order_by(AuditLog.performed_at.desc()).limit(5000).all()

    # Build PDF
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            leftMargin=0.5*inch, rightMargin=0.5*inch,
                            topMargin=0.5*inch, bottomMargin=0.5*inch)
    styles = getSampleStyleSheet()
    small_style = ParagraphStyle('Small', parent=styles['Normal'], fontSize=7,
                                 leading=9)
    elements = []

    # Title
    elements.append(Paragraph('SMS Audit Log Report', styles['Title']))
    elements.append(Spacer(1, 12))

    # Metadata
    now_str = jamaica_now().strftime('%d %B %Y at %I:%M %p')
    elements.append(Paragraph(
        f'<b>Generated:</b> {now_str}', styles['Normal']))
    elements.append(Paragraph(
        f'<b>Generated By:</b> {current_user.full_name}', styles['Normal']))
    elements.append(Spacer(1, 6))

    # Filter summary
    filters_applied = []
    if q_date_from:
        filters_applied.append(f'From: {q_date_from}')
    if q_date_to:
        filters_applied.append(f'To: {q_date_to}')
    if q_user:
        filters_applied.append(f'User: {q_user}')
    if q_role:
        filters_applied.append(f'Role: {q_role}')
    if q_report_type:
        filters_applied.append(f'Report Type: {q_report_type}')
    if q_action:
        filters_applied.append(f'Action: {q_action}')
    if q_stage:
        filters_applied.append(f'Stage: {q_stage}')
    if q_keyword:
        filters_applied.append(f'Keyword: {q_keyword}')
    if q_entity:
        filters_applied.append(f'Entity: {q_entity}')

    if filters_applied:
        elements.append(Paragraph(
            f'<b>Filters:</b> {"; ".join(filters_applied)}', styles['Normal']))
    else:
        elements.append(Paragraph('<b>Filters:</b> None (all entries)', styles['Normal']))
    elements.append(Paragraph(
        f'<b>Total Entries:</b> {len(entries)}', styles['Normal']))
    elements.append(Spacer(1, 18))

    # Table data
    header = ['#', 'Date/Time', 'Action', 'User', 'Entity', 'Description']
    data = [header]
    for i, entry in enumerate(entries, 1):
        dt = entry.performed_at.strftime('%d %b %Y %H:%M') if entry.performed_at else ''
        user_name = entry.performer.full_name if entry.performer else '—'
        entity = entry.entity_label or str(entry.entity_id or '')
        desc = entry.human_description or entry.action.replace('_', ' ')
        # Truncate long descriptions for PDF
        if len(desc) > 120:
            desc = desc[:117] + '...'
        data.append([
            str(i),
            Paragraph(dt, small_style),
            Paragraph(entry.action.replace('_', ' '), small_style),
            Paragraph(user_name, small_style),
            Paragraph(entity, small_style),
            Paragraph(desc, small_style),
        ])

    col_widths = [0.4*inch, 1.2*inch, 1.5*inch, 1.5*inch, 1.2*inch, 4*inch]
    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0d6efd')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, 0), 8),
        ('FONTSIZE', (0, 1), (-1, -1), 7),
        ('ALIGN', (0, 0), (0, -1), 'CENTER'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')]),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    elements.append(table)

    doc.build(elements)
    buf.seek(0)

    # Log the export action
    db.session.add(AuditLog(
        action='AUDIT_LOG_EXPORT',
        entity_type='AuditLog',
        entity_id=None,
        entity_label='PDF Export',
        details=json.dumps({
            'exported_by': current_user.full_name,
            'record_count': len(entries),
            'filters': filters_applied,
        }),
        performed_by=current_user.id,
        human_description=(
            f'{current_user.full_name} exported Audit Log to PDF '
            f'({len(entries)} entries). '
            f'Filters: {"; ".join(filters_applied) if filters_applied else "None"}'
        ),
        ip_address=request.remote_addr,
        user_agent=request.headers.get('User-Agent', '')[:500],
        success=True,
    ))
    db.session.commit()

    return Response(
        buf.getvalue(),
        mimetype='application/pdf',
        headers={
            'Content-Disposition': f'attachment; filename="Audit_Log_{jamaica_now().strftime("%Y%m%d")}.pdf"'
        },
    )


# ---------------------------------------------------------------------------
# Quality Control Data Downloads
# ---------------------------------------------------------------------------

@main_bp.route('/admin/qc-downloads')
@login_required
def qc_downloads():
    """Quality Control data download page."""
    if not (current_user.has_any_role(Role.ADMIN, Role.SUPER_ADMIN)
            or current_user.has_permission(Permission.QC_DATA_DOWNLOAD)):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))
    return render_template('admin/qc_downloads.html')


@main_bp.route('/admin/qc-downloads/export', methods=['POST'])
@login_required
def qc_downloads_export():
    """Execute a QC data download."""
    if not (current_user.has_any_role(Role.ADMIN, Role.SUPER_ADMIN)
            or current_user.has_permission(Permission.QC_DATA_DOWNLOAD)):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    data_type = request.form.get('data_type', '').strip()
    export_format = request.form.get('export_format', 'csv').strip()
    date_from = request.form.get('date_from', '').strip()
    date_to = request.form.get('date_to', '').strip()

    if export_format not in ('csv', 'pdf'):
        export_format = 'csv'

    valid_types = [
        'user_permissions', 'workflow_logs', 'login_activity',
        'report_activity', 'export_activity', 'error_logs',
    ]
    if data_type not in valid_types:
        flash('Invalid data type selected.', 'danger')
        return redirect(url_for('main.qc_downloads'))

    # Parse date filters
    dt_from = None
    dt_to = None
    if date_from:
        try:
            dt_from = datetime.strptime(date_from, '%Y-%m-%d')
        except ValueError:
            pass
    if date_to:
        try:
            dt_to = datetime.strptime(date_to, '%Y-%m-%d').replace(
                hour=23, minute=59, second=59)
        except ValueError:
            pass

    buf = io.StringIO()
    writer = csv.writer(buf)
    record_count = 0

    if data_type == 'user_permissions':
        writer.writerow(['Username', 'Full Name', 'Email', 'Roles', 'Branches',
                         'Permissions', 'Active', 'Last Seen'])
        users = User.query.order_by(User.last_name).all()
        for u in users:
            roles = ', '.join(r.value for r in u.roles) if u.roles else ''
            branches = ', '.join(b.value for b in u.branches) if u.branches else ''
            perms = ', '.join(p.value for p in u.permissions) if u.permissions else ''
            writer.writerow([
                u.username, u.full_name, u.email, roles, branches, perms,
                'Yes' if u.is_active_user else 'No',
                u.last_seen.strftime('%Y-%m-%d %H:%M') if u.last_seen else '',
            ])
            record_count += 1

    elif data_type == 'workflow_logs':
        writer.writerow(['Date/Time', 'Action', 'Entity Type', 'Entity',
                         'User', 'Role', 'Report Type', 'Previous Stage',
                         'New Stage', 'Comments', 'Description'])
        q = AuditLog.query
        if dt_from:
            q = q.filter(AuditLog.performed_at >= dt_from)
        if dt_to:
            q = q.filter(AuditLog.performed_at <= dt_to)
        entries = q.order_by(AuditLog.performed_at.desc()).limit(10000).all()
        for e in entries:
            writer.writerow([
                e.performed_at.strftime('%Y-%m-%d %H:%M') if e.performed_at else '',
                e.action, e.entity_type, e.entity_label or '',
                e.performer.full_name if e.performer else '',
                e.user_role or '', e.report_type or '',
                e.previous_stage or '', e.new_stage or '',
                e.comments or '',
                e.human_description or '',
            ])
            record_count += 1

    elif data_type == 'login_activity':
        writer.writerow(['Username', 'Full Name', 'Last Seen',
                         'Failed Attempts', 'Locked Until', 'Active'])
        users = User.query.order_by(User.last_seen.desc().nullslast()).all()
        for u in users:
            writer.writerow([
                u.username, u.full_name,
                u.last_seen.strftime('%Y-%m-%d %H:%M') if u.last_seen else 'Never',
                u.failed_login_attempts or 0,
                u.locked_until.strftime('%Y-%m-%d %H:%M') if u.locked_until else '',
                'Yes' if u.is_active_user else 'No',
            ])
            record_count += 1

    elif data_type == 'report_activity':
        writer.writerow(['Date/Time', 'Action', 'Lab Number', 'Sample Name',
                         'Report Type', 'User', 'Status Change'])
        q = AuditLog.query.filter(AuditLog.entity_type == 'Sample')
        if dt_from:
            q = q.filter(AuditLog.performed_at >= dt_from)
        if dt_to:
            q = q.filter(AuditLog.performed_at <= dt_to)
        entries = q.order_by(AuditLog.performed_at.desc()).limit(10000).all()
        for e in entries:
            stage_change = ''
            if e.previous_stage and e.new_stage:
                stage_change = f'{e.previous_stage} → {e.new_stage}'
            writer.writerow([
                e.performed_at.strftime('%Y-%m-%d %H:%M') if e.performed_at else '',
                e.action, e.entity_label or '', '',
                e.report_type or '',
                e.performer.full_name if e.performer else '',
                stage_change,
            ])
            record_count += 1

    elif data_type == 'export_activity':
        writer.writerow(['Date/Time', 'Action', 'User', 'Details'])
        q = AuditLog.query.filter(
            db.or_(
                AuditLog.action.ilike('%EXPORT%'),
                AuditLog.action.ilike('%DOWNLOAD%'),
            )
        )
        if dt_from:
            q = q.filter(AuditLog.performed_at >= dt_from)
        if dt_to:
            q = q.filter(AuditLog.performed_at <= dt_to)
        entries = q.order_by(AuditLog.performed_at.desc()).limit(10000).all()
        for e in entries:
            writer.writerow([
                e.performed_at.strftime('%Y-%m-%d %H:%M') if e.performed_at else '',
                e.action,
                e.performer.full_name if e.performer else '',
                e.human_description or e.details or '',
            ])
            record_count += 1

    elif data_type == 'error_logs':
        # Return empty – error logs from Python logging are not in DB
        writer.writerow(['Note'])
        writer.writerow(['System error logs are stored in application log files. '
                         'Contact system administrator for server-level logs.'])
        record_count = 0

    # Log the download
    filters_used = []
    if date_from:
        filters_used.append(f'from={date_from}')
    if date_to:
        filters_used.append(f'to={date_to}')

    db.session.add(AuditLog(
        action='QC_DATA_DOWNLOAD',
        entity_type='SystemExport',
        entity_id=None,
        entity_label=f'{data_type} Export',
        details=json.dumps({
            'data_type': data_type,
            'format': export_format,
            'record_count': record_count,
            'filters': filters_used,
        }),
        performed_by=current_user.id,
        human_description=(
            f'{current_user.full_name} downloaded {data_type.replace("_", " ").title()} '
            f'data ({record_count} records) in {export_format.upper()} format.'
        ),
        ip_address=request.remote_addr,
        user_agent=request.headers.get('User-Agent', '')[:500],
        success=True,
    ))
    db.session.commit()

    filename = f'QC_{data_type}_{jamaica_now().strftime("%Y%m%d")}'

    if export_format == 'pdf':
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib import colors
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        )
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

        # Parse the CSV buffer to extract rows for the PDF
        buf.seek(0)
        reader = csv.reader(buf)
        all_rows = list(reader)

        pdf_buf = io.BytesIO()
        doc = SimpleDocTemplate(pdf_buf, pagesize=landscape(A4),
                                leftMargin=0.5*inch, rightMargin=0.5*inch,
                                topMargin=0.5*inch, bottomMargin=0.5*inch)
        styles = getSampleStyleSheet()
        small_style = ParagraphStyle('Small', parent=styles['Normal'], fontSize=7,
                                     leading=9)
        elements = []

        # Title
        title_text = data_type.replace('_', ' ').title()
        elements.append(Paragraph(f'QC Data Export – {title_text}', styles['Title']))
        elements.append(Spacer(1, 12))

        # Metadata
        now_str = jamaica_now().strftime('%d %B %Y at %I:%M %p')
        elements.append(Paragraph(
            f'<b>Generated:</b> {now_str}', styles['Normal']))
        elements.append(Paragraph(
            f'<b>Generated By:</b> {current_user.full_name}', styles['Normal']))
        elements.append(Paragraph(
            f'<b>Records:</b> {record_count}', styles['Normal']))
        elements.append(Spacer(1, 12))

        if all_rows:
            # Wrap cell content in Paragraphs for word-wrapping
            table_data = []
            for row in all_rows:
                table_data.append([Paragraph(str(cell), small_style) for cell in row])

            col_count = len(all_rows[0]) if all_rows else 1
            available_width = landscape(A4)[0] - 1 * inch
            col_width = available_width / col_count
            col_widths = [col_width] * col_count

            t = Table(table_data, colWidths=col_widths, repeatRows=1)
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('FONTSIZE', (0, 0), (-1, -1), 7),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')]),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ]))
            elements.append(t)

        doc.build(elements)
        pdf_buf.seek(0)

        return Response(
            pdf_buf.getvalue(),
            mimetype='application/pdf',
            headers={'Content-Disposition': f'attachment; filename="{filename}.pdf"'},
        )

    # Default: CSV
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}.csv"'},
    )
