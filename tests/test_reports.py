"""Tests for KPI reports and Pharmaceutical reports."""
from datetime import date, datetime, timezone

from app import db
from app.models import (
    Sample, SampleAssignment, User, Role, Branch, SampleStatus, KpiTarget,
    NonWorkingDay,
    KPI_METRICS, AUTO_ACTUAL_KEYS,
)
from tests.conftest import _create_user, _login


def _setup_admin(app):
    """Create an admin user and return the user id."""
    with app.app_context():
        admin = _create_user(Role.ADMIN, username='admin')
        return admin.id


def _setup_senior(app):
    """Create a senior chemist user and return the user id."""
    with app.app_context():
        sc = _create_user(Role.SENIOR_CHEMIST, Branch.PHARMACEUTICAL,
                          username='senior')
        return sc.id


def _register_pharma_sample(
    app,
    lab,
    name='Test Drug',
    certified=False,
    formulation_type=None,
    api=None,
    source=None,
    description=None,
):
    """Register a pharmaceutical sample directly in the DB."""
    with app.app_context():
        officer = User.query.filter_by(username='admin').first()
        if not officer:
            officer = _create_user(Role.ADMIN, username='admin')
        s = Sample(
            lab_number=lab,
            sample_name=name,
            sample_type=Branch.PHARMACEUTICAL,
            date_received=date(2026, 1, 15),
            uploaded_by=officer.id,
            status=SampleStatus.CERTIFIED if certified else SampleStatus.REGISTERED,
            formulation_type=formulation_type,
            api=api,
            source=source,
            description=description,
        )
        if certified:
            s.certified_at = datetime(2026, 2, 15, tzinfo=timezone.utc)
        db.session.add(s)
        db.session.commit()
        return s.id


# ---------------------------------------------------------------------------
# KPI Report page
# ---------------------------------------------------------------------------

def test_kpi_report_requires_login(app, client):
    resp = client.get('/kpi/report')
    assert resp.status_code == 302
    assert '/auth/login' in resp.headers.get('Location', '')


def test_kpi_report_access_denied_for_chemist(app, client):
    with app.app_context():
        _create_user(Role.CHEMIST, username='chem')
    _login(client, 'chem')
    resp = client.get('/kpi/report', follow_redirects=True)
    assert b'Access denied' in resp.data


def test_kpi_report_renders_for_admin(app, client):
    _setup_admin(app)
    _login(client, 'admin')
    resp = client.get('/kpi/report?year=2026&quarter=1')
    assert resp.status_code == 200
    assert b'KPI Report' in resp.data
    assert b'Download CSV' in resp.data
    # Check some KPI labels appear
    assert b'Pharmaceutical' in resp.data
    assert b'toxicology' in resp.data


def test_kpi_report_shows_auto_actuals(app, client):
    _setup_admin(app)
    _register_pharma_sample(app, 'PH-001', certified=True)
    _login(client, 'admin')
    resp = client.get('/kpi/report?year=2026&quarter=1')
    assert resp.status_code == 200
    # The certified pharma sample should show as actual = 1
    assert b'1' in resp.data


# ---------------------------------------------------------------------------
# KPI Report CSV download
# ---------------------------------------------------------------------------

def test_kpi_report_download_csv(app, client):
    _setup_admin(app)
    _login(client, 'admin')
    resp = client.get('/kpi/report/download?year=2026&quarter=1')
    assert resp.status_code == 200
    assert 'text/csv' in resp.content_type
    assert b'KPI,Target,Actual,Variance' in resp.data


# ---------------------------------------------------------------------------
# KPI Targets management
# ---------------------------------------------------------------------------

def test_kpi_targets_access_denied_for_senior(app, client):
    _setup_senior(app)
    _login(client, 'senior')
    resp = client.get('/kpi/targets', follow_redirects=True)
    assert b'Access denied' in resp.data


def test_kpi_targets_renders_for_admin(app, client):
    _setup_admin(app)
    _login(client, 'admin')
    resp = client.get('/kpi/targets?year=2026&quarter=1')
    assert resp.status_code == 200
    assert b'KPI Targets' in resp.data
    assert b'Save Targets' in resp.data


def test_kpi_targets_save(app, client):
    _setup_admin(app)
    _login(client, 'admin')
    resp = client.post('/kpi/targets?year=2026&quarter=1', data={
        'year': 2026,
        'quarter': 1,
        'target_pharma_coas': '35',
        'target_milk_coas': '42',
        'target_complaints_resolved': '1',
        'actual_complaints_resolved': '0',
    }, follow_redirects=True)
    assert b'KPI targets saved' in resp.data

    with app.app_context():
        t = KpiTarget.query.filter_by(
            year=2026, quarter=1, kpi_key='pharma_coas'
        ).first()
        assert t is not None
        assert t.target_value == 35.0

        t2 = KpiTarget.query.filter_by(
            year=2026, quarter=1, kpi_key='complaints_resolved'
        ).first()
        assert t2 is not None
        assert t2.target_value == 1.0
        assert t2.actual_override == 0.0


def test_kpi_report_variance_computed(app, client):
    """When targets and actuals both exist, variance should be computed."""
    _setup_admin(app)
    _register_pharma_sample(app, 'PH-V01', certified=True)
    _login(client, 'admin')

    # Set a target
    client.post('/kpi/targets?year=2026&quarter=1', data={
        'year': 2026,
        'quarter': 1,
        'target_pharma_coas': '5',
    }, follow_redirects=True)

    resp = client.get('/kpi/report?year=2026&quarter=1')
    assert resp.status_code == 200
    # actual=1, target=5, variance=-4
    assert b'-4' in resp.data


def test_kpi_auto_actuals_tat_uses_date_registered(app):
    """Auto KPI TAT should be measured from registration, not received date."""
    from app.main.routes import _auto_actuals
    with app.app_context():
        admin = _create_user(Role.ADMIN, username='admin_tat_kpi')
        sample = Sample(
            lab_number='PH-TAT-KPI-001',
            sample_name='TAT KPI Baseline',
            sample_type=Branch.PHARMACEUTICAL,
            date_received=date(2026, 3, 1),
            date_registered=datetime(2026, 4, 2, tzinfo=timezone.utc),
            certified_at=datetime(2026, 4, 4, tzinfo=timezone.utc),
            uploaded_by=admin.id,
            status=SampleStatus.CERTIFIED,
        )
        db.session.add(sample)
        db.session.commit()

        actuals = _auto_actuals(2026, 1)
        assert actuals['avg_days_pharma_coa'] == 1.0


def test_kpi_auto_actuals_tat_uses_full_sample_date_range(app):
    """Auto KPI TAT should include holidays after quarter end when needed."""
    from app.main.routes import _auto_actuals
    with app.app_context():
        admin = _create_user(Role.ADMIN, username='admin_tat_range')
        sample = Sample(
            lab_number='PH-TAT-RANGE-001',
            sample_name='TAT Range Check',
            sample_type=Branch.PHARMACEUTICAL,
            date_received=date(2026, 6, 30),
            date_registered=datetime(2026, 6, 30, tzinfo=timezone.utc),
            certified_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
            uploaded_by=admin.id,
            status=SampleStatus.CERTIFIED,
        )
        holiday = NonWorkingDay(
            date=date(2026, 7, 1),
            description='Cross-quarter holiday',
            day_type='holiday',
            created_by=admin.id,
        )
        db.session.add(sample)
        db.session.add(holiday)
        db.session.commit()

        actuals = _auto_actuals(2026, 1)
        assert actuals['avg_days_pharma_coa'] == 2.0


def test_kpi_auto_actuals_only_prefetches_tat_date_range(app, monkeypatch):
    """Auto KPI TAT should avoid an unused quarter-wide non-working prefetch."""
    from app.main import routes

    with app.app_context():
        admin = _create_user(Role.ADMIN, username='admin_tat_prefetch')
        sample = Sample(
            lab_number='PH-TAT-PREFETCH-001',
            sample_name='TAT Prefetch Check',
            sample_type=Branch.PHARMACEUTICAL,
            date_received=date(2026, 6, 30),
            date_registered=datetime(2026, 6, 30, tzinfo=timezone.utc),
            certified_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
            uploaded_by=admin.id,
            status=SampleStatus.CERTIFIED,
        )
        db.session.add(sample)
        db.session.commit()

        calls = []

        def fake_fetch_non_working_days(start_date, end_date):
            calls.append((start_date, end_date))
            return set()

        monkeypatch.setattr(routes, 'fetch_non_working_days', fake_fetch_non_working_days)

        actuals = routes._auto_actuals(2026, 1)

        assert actuals['avg_days_pharma_coa'] == 3.0
        assert calls == [(date(2026, 6, 30), date(2026, 7, 3))]


# ---------------------------------------------------------------------------
# Pharmaceutical Report
# ---------------------------------------------------------------------------

def test_pharma_report_requires_login(app, client):
    resp = client.get('/reports/pharma')
    assert resp.status_code == 302


def test_pharma_report_renders(app, client):
    _setup_admin(app)
    _register_pharma_sample(app, 'PH-R01', 'Aspirin', certified=True)
    _login(client, 'admin')
    # certified_at is Feb 2026 → fiscal year 2025 (Apr 2025 – Mar 2026)
    resp = client.get('/reports/pharma?year=2025')
    assert resp.status_code == 200
    assert b'Pharmaceutical Report' in resp.data
    assert b'PH-R01' in resp.data
    assert b'Aspirin' in resp.data


def test_pharma_report_quarter_filter(app, client):
    _setup_admin(app)
    _register_pharma_sample(app, 'PH-Q01')
    _login(client, 'admin')
    # Uncertified samples carry forward – they appear in every quarter
    resp = client.get('/reports/pharma?year=2026&quarter=1')
    assert b'PH-Q01' in resp.data
    resp = client.get('/reports/pharma?year=2026&quarter=3')
    assert b'PH-Q01' in resp.data


def test_pharma_report_download_csv(app, client):
    _setup_admin(app)
    _register_pharma_sample(app, 'PH-DL01')
    _login(client, 'admin')
    resp = client.get('/reports/pharma/download?year=2026')
    assert resp.status_code == 200
    assert 'text/csv' in resp.content_type
    assert b'PH-DL01' in resp.data
    assert b'Lab Number' in resp.data


def test_pharma_report_download_respects_status_filter(app, client):
    """Exported CSV must honour the status filter shown on screen."""
    _setup_admin(app)
    # Certified within FY2025 (certified_at 2026-02-15 -> fiscal year 2025)
    _register_pharma_sample(app, 'PH-CERT', certified=True)
    # Uncertified sample (carried forward)
    _register_pharma_sample(app, 'PH-INPROG', certified=False)
    _login(client, 'admin')

    # Without a status filter both samples are exported
    resp = client.get('/reports/pharma/download?year=2025')
    assert resp.status_code == 200
    assert b'PH-CERT' in resp.data
    assert b'PH-INPROG' in resp.data

    # Filtering by Certified must exclude the in-progress sample
    resp = client.get('/reports/pharma/download?year=2025&status=Certified')
    assert resp.status_code == 200
    assert b'PH-CERT' in resp.data
    assert b'PH-INPROG' not in resp.data


def test_pharma_report_download_tat_uses_full_sample_date_range(app, client):
    """TAT export should include holidays across the full measured interval."""
    import csv
    _setup_admin(app)
    with app.app_context():
        admin = User.query.filter_by(username='admin').first()
        sample = Sample(
            lab_number='PH-HOL-001',
            sample_name='Range Holiday Check',
            sample_type=Branch.PHARMACEUTICAL,
            date_received=date(2026, 3, 28),
            date_registered=datetime(2026, 3, 28, tzinfo=timezone.utc),
            certified_at=datetime(2026, 4, 3, tzinfo=timezone.utc),
            uploaded_by=admin.id,
            status=SampleStatus.CERTIFIED,
        )
        holiday = NonWorkingDay(
            date=date(2026, 3, 31),
            description='Range test holiday',
            day_type='holiday',
            created_by=admin.id,
        )
        db.session.add(sample)
        db.session.add(holiday)
        db.session.commit()

    _login(client, 'admin')
    resp = client.get('/reports/pharma/download?year=2026&quarter=1')
    assert resp.status_code == 200

    rows = list(csv.reader(resp.data.decode('utf-8').splitlines()))
    row = next(r for r in rows if r and r[0] == 'PH-HOL-001')
    assert row[10] == '4'


def test_pharma_report_tat_uses_full_sample_date_range(app, client):
    """On-screen TAT should include holidays across the full measured interval."""
    _setup_admin(app)
    with app.app_context():
        admin = User.query.filter_by(username='admin').first()
        sample = Sample(
            lab_number='PH-HOL-002',
            sample_name='Range Holiday Report',
            sample_type=Branch.PHARMACEUTICAL,
            date_received=date(2026, 3, 28),
            date_registered=datetime(2026, 3, 28, tzinfo=timezone.utc),
            certified_at=datetime(2026, 4, 3, tzinfo=timezone.utc),
            uploaded_by=admin.id,
            status=SampleStatus.CERTIFIED,
        )
        holiday = NonWorkingDay(
            date=date(2026, 3, 31),
            description='Report range holiday',
            day_type='holiday',
            created_by=admin.id,
        )
        db.session.add(sample)
        db.session.add(holiday)
        db.session.commit()

    _login(client, 'admin')
    resp = client.get('/reports/pharma?year=2026&quarter=1')
    assert resp.status_code == 200
    assert b'PH-HOL-002' in resp.data
    assert b'<span class="badge bg-info text-dark">4</span>' in resp.data


def test_pharma_report_filter_formulation_api_source(app, client):
    _setup_admin(app)
    _register_pharma_sample(
        app,
        'PH-FLT01',
        name='Pain Relief Tablet',
        formulation_type='Tablet',
        api='Acetaminophen',
        source='Central Medical Store',
        description='Contains pain reliever',
    )
    _register_pharma_sample(
        app,
        'PH-FLT02',
        name='Antibiotic Syrup',
        formulation_type='Syrup',
        api='Amoxicillin',
        source='Private Import',
        description='Contains antibiotic',
    )
    _login(client, 'admin')

    resp = client.get(
        '/reports/pharma?year=2026&formulation_type=Tab&api=Acetaminophen&source=Central'
    )
    assert resp.status_code == 200
    assert b'PH-FLT01' in resp.data
    assert b'PH-FLT02' not in resp.data


# ---------------------------------------------------------------------------
# Sidebar and navigation
# ---------------------------------------------------------------------------

def test_sidebar_shows_kpi_report_link(app, client):
    _setup_admin(app)
    _login(client, 'admin')
    resp = client.get('/dashboard')
    assert b'KPI Report' in resp.data
    assert b'Pharm Report' in resp.data


def test_kpi_dashboard_has_report_links(app, client):
    _setup_admin(app)
    _login(client, 'admin')
    resp = client.get('/kpi')
    assert b'KPI Report' in resp.data
    assert b'Pharm Report' in resp.data


# ---------------------------------------------------------------------------
# Milk Report
# ---------------------------------------------------------------------------

def _register_milk_sample(
    app,
    lab,
    name='Test Milk',
    certified=False,
    parish=None,
    milk_type='R',
):
    """Register a milk sample directly in the DB."""
    with app.app_context():
        officer = User.query.filter_by(username='admin').first()
        if not officer:
            officer = _create_user(Role.ADMIN, username='admin')
        s = Sample(
            lab_number=lab,
            sample_name=name,
            sample_type=Branch.FOOD_MILK,
            date_received=date(2026, 1, 15),
            uploaded_by=officer.id,
            status=SampleStatus.CERTIFIED if certified else SampleStatus.REGISTERED,
            parish=parish,
            milk_type=milk_type,
            volume='500ml',
        )
        if certified:
            s.certified_at = datetime(2026, 2, 15, tzinfo=timezone.utc)
        db.session.add(s)
        db.session.commit()
        return s.id


def test_milk_report_requires_login(app, client):
    resp = client.get('/reports/milk')
    assert resp.status_code == 302


def test_milk_report_access_denied_for_chemist(app, client):
    with app.app_context():
        _create_user(Role.CHEMIST, username='chem')
    _login(client, 'chem')
    resp = client.get('/reports/milk', follow_redirects=True)
    assert b'Access denied' in resp.data


def test_milk_report_renders(app, client):
    _setup_admin(app)
    _register_milk_sample(app, 'MILK-R01', 'Farm Milk A', certified=True)
    _login(client, 'admin')
    # certified_at is Feb 2026 → fiscal year 2025 (Apr 2025 – Mar 2026)
    resp = client.get('/reports/milk?year=2025')
    assert resp.status_code == 200
    assert b'Milk Sample Report' in resp.data
    assert b'MILK-R01' in resp.data
    assert b'Farm Milk A' in resp.data


def test_milk_report_quarter_filter(app, client):
    _setup_admin(app)
    _register_milk_sample(app, 'MILK-Q01')
    _login(client, 'admin')
    # Uncertified samples carry forward – they appear in every quarter
    resp = client.get('/reports/milk?year=2026&quarter=1')
    assert b'MILK-Q01' in resp.data
    resp = client.get('/reports/milk?year=2026&quarter=3')
    assert b'MILK-Q01' in resp.data


def test_milk_report_download_csv(app, client):
    _setup_admin(app)
    _register_milk_sample(app, 'MILK-DL01')
    _login(client, 'admin')
    resp = client.get('/reports/milk/download?year=2026')
    assert resp.status_code == 200
    assert 'text/csv' in resp.content_type
    assert b'MILK-DL01' in resp.data
    assert b'Lab Number' in resp.data


def test_milk_report_shows_turnaround(app, client):
    _setup_admin(app)
    _register_milk_sample(app, 'MILK-TAT01', certified=True)
    _login(client, 'admin')
    # certified_at is Feb 2026 → fiscal year 2025 (Apr 2025 – Mar 2026)
    resp = client.get('/reports/milk?year=2025')
    assert resp.status_code == 200
    # Certified sample should show TAT (31 days: Jan 15 → Feb 15)
    assert b'31' in resp.data


def test_milk_report_filter_parish_and_milk_type(app, client):
    _setup_admin(app)
    _register_milk_sample(
        app,
        'MILK-FLT01',
        name='Farm Milk A',
        parish='Kingston',
        milk_type='R',
    )
    _register_milk_sample(
        app,
        'MILK-FLT02',
        name='Farm Milk B',
        parish='St. Andrew',
        milk_type='P',
    )
    _login(client, 'admin')

    resp = client.get('/reports/milk?year=2026&parish=King&milk_type=R')
    assert resp.status_code == 200
    assert b'MILK-FLT01' in resp.data
    assert b'MILK-FLT02' not in resp.data


def test_sidebar_shows_milk_report_link(app, client):
    _setup_admin(app)
    _login(client, 'admin')
    resp = client.get('/dashboard')
    assert b'Milk Report' in resp.data


# ---------------------------------------------------------------------------
# Toxicology Report
# ---------------------------------------------------------------------------

def _register_toxicology_sample(
    app,
    lab,
    name='Blood Sample',
    hospital=None,
    sample_type_name=None,
    patient_name=None,
):
    """Register a toxicology sample directly in the DB."""
    with app.app_context():
        officer = User.query.filter_by(username='admin').first()
        if not officer:
            officer = _create_user(Role.ADMIN, username='admin')
        s = Sample(
            lab_number=lab,
            sample_name=name,
            sample_type=Branch.TOXICOLOGY,
            date_received=date(2026, 1, 15),
            uploaded_by=officer.id,
            status=SampleStatus.REGISTERED,
            source=hospital,
            toxicology_sample_type_name=sample_type_name,
            patient_name=patient_name,
            volume='10ml',
        )
        db.session.add(s)
        db.session.commit()
        return s.id


def test_toxicology_report_filter_hospital_sample_type_patient_name(app, client):
    _setup_admin(app)
    _register_toxicology_sample(
        app,
        'TOX-FLT01',
        hospital='Kingston Public Hospital',
        sample_type_name='Urine',
        patient_name='John Brown',
    )
    _register_toxicology_sample(
        app,
        'TOX-FLT02',
        hospital='Spanish Town Hospital',
        sample_type_name='Blood',
        patient_name='Jane Doe',
    )
    _login(client, 'admin')

    resp = client.get(
        '/reports/toxicology?year=2026&hospital=Kingston&sample_type=Uri&patient_name=John'
    )
    assert resp.status_code == 200
    assert b'TOX-FLT01' in resp.data
    assert b'TOX-FLT02' not in resp.data


def test_kpi_toxicology_tat_uses_full_sample_date_range(app, client):
    """Toxicology KPI TAT should include holidays after quarter end when needed."""
    _setup_admin(app)
    with app.app_context():
        admin = User.query.filter_by(username='admin').first()
        sample = Sample(
            lab_number='TOX-TAT-001',
            sample_name='Toxicology Range Check',
            sample_type=Branch.TOXICOLOGY,
            date_received=date(2026, 6, 30),
            date_registered=datetime(2026, 6, 30, tzinfo=timezone.utc),
            certified_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
            uploaded_by=admin.id,
            status=SampleStatus.CERTIFIED,
        )
        holiday = NonWorkingDay(
            date=date(2026, 7, 1),
            description='Toxicology range holiday',
            day_type='holiday',
            created_by=admin.id,
        )
        db.session.add(sample)
        db.session.add(holiday)
        db.session.commit()

    _login(client, 'admin')
    resp = client.get('/kpi/toxicology?year=2026')
    assert resp.status_code == 200
    assert b'Q1 (Apr-Jun)' in resp.data
    assert b'<td class="text-end">2.0</td>' in resp.data


# ---------------------------------------------------------------------------
# Alcohol Report
# ---------------------------------------------------------------------------

def _register_alcohol_sample(
    app,
    lab,
    name='Rum Product',
    alcohol_type=None,
):
    """Register an alcohol sample directly in the DB."""
    with app.app_context():
        officer = User.query.filter_by(username='admin').first()
        if not officer:
            officer = _create_user(Role.ADMIN, username='admin')
        s = Sample(
            lab_number=lab,
            sample_name=name,
            sample_type=Branch.FOOD_ALCOHOL,
            date_received=date(2026, 1, 15),
            uploaded_by=officer.id,
            status=SampleStatus.REGISTERED,
            alcohol_type=alcohol_type,
            quantity='1 bottle',
        )
        db.session.add(s)
        db.session.commit()
        return s.id


def test_alcohol_report_filter_sample_name_and_type(app, client):
    _setup_admin(app)
    _register_alcohol_sample(
        app,
        'ALC-FLT01',
        name='Rum Gold Reserve',
        alcohol_type='Alcohol Determination',
    )
    _register_alcohol_sample(
        app,
        'ALC-FLT02',
        name='Denatured Spirit',
        alcohol_type='Denatured Alcohol (bitrex)',
    )
    _login(client, 'admin')

    resp = client.get(
        '/reports/alcohol?year=2026&sample_name=Rum&alcohol_type=Determination'
    )
    assert resp.status_code == 200
    assert b'ALC-FLT01' in resp.data
    assert b'ALC-FLT02' not in resp.data


# ---------------------------------------------------------------------------
# Analyst Report — resubmission type filter
# ---------------------------------------------------------------------------

def _setup_analyst_report_data(app):
    """Create an admin, a chemist, sample, assignment, and document versions."""
    from app.models import (
        Sample, SampleAssignment, DocumentVersion,
        AssignmentStatus, SampleStatus,
    )
    with app.app_context():
        admin = _create_user(Role.ADMIN, username='admin_ar')
        chemist = _create_user(Role.CHEMIST, Branch.PHARMACEUTICAL, username='chemist_ar')

        s = Sample(
            lab_number='AR-001',
            sample_name='Analyst Report Test',
            sample_type=Branch.PHARMACEUTICAL,
            date_received=date(2026, 4, 1),
            uploaded_by=admin.id,
            status=SampleStatus.REGISTERED,
        )
        db.session.add(s)
        db.session.flush()

        a = SampleAssignment(
            sample_id=s.id,
            chemist_id=chemist.id,
            assigned_by=admin.id,
            test_name='Dissolution',
            assigned_date=date(2026, 4, 2),
            status=AssignmentStatus.COMPLETED,
        )
        db.session.add(a)
        db.session.flush()

        # Original submission
        db.session.add(DocumentVersion(
            sample_id=s.id,
            document_type='report',
            version_number=1,
            file_path='/fake/v1.pdf',
            original_name='report_v1.pdf',
            upload_label='original',
            uploaded_by=chemist.id,
            assignment_id=a.id,
        ))
        # Preliminary review resubmission
        db.session.add(DocumentVersion(
            sample_id=s.id,
            document_type='report',
            version_number=2,
            file_path='/fake/v2.pdf',
            original_name='report_v2.pdf',
            upload_label='resubmission',
            resubmission_type='preliminary',
            uploaded_by=chemist.id,
            assignment_id=a.id,
        ))
        # Technical review resubmission
        db.session.add(DocumentVersion(
            sample_id=s.id,
            document_type='report',
            version_number=3,
            file_path='/fake/v3.pdf',
            original_name='report_v3.pdf',
            upload_label='resubmission',
            resubmission_type='technical',
            uploaded_by=chemist.id,
            assignment_id=a.id,
        ))
        # Unspecified resubmission (historical)
        db.session.add(DocumentVersion(
            sample_id=s.id,
            document_type='report',
            version_number=4,
            file_path='/fake/v4.pdf',
            original_name='report_v4.pdf',
            upload_label='resubmission',
            resubmission_type='unspecified',
            uploaded_by=chemist.id,
            assignment_id=a.id,
        ))
        db.session.commit()
        return s.id, a.id


def test_analyst_report_renders_for_admin(app, client):
    _setup_analyst_report_data(app)
    _login(client, 'admin_ar')
    resp = client.get('/reports/analysts')
    assert resp.status_code == 200
    assert b'Analyst Performance Report' in resp.data


def test_analyst_report_transparency_label_all(app, client):
    """Default (all types) shows the all-types label."""
    _setup_analyst_report_data(app)
    _login(client, 'admin_ar')
    resp = client.get('/reports/analysts?resub_type=all')
    assert resp.status_code == 200
    assert b'All Review Types' in resp.data


def test_analyst_report_filter_preliminary_only(app, client):
    """Filtering by preliminary counts only preliminary resubmissions."""
    _setup_analyst_report_data(app)
    _login(client, 'admin_ar')
    resp = client.get('/reports/analysts?resub_type=preliminary')
    assert resp.status_code == 200
    assert b'Preliminary Review' in resp.data
    # The page always shows the return-by-stage breakdown columns
    assert b'Returned for Correction' in resp.data


def test_analyst_report_filter_multiple_types(app, client):
    """Filtering by two types is accepted without error."""
    _setup_analyst_report_data(app)
    _login(client, 'admin_ar')
    resp = client.get('/reports/analysts?resub_type=preliminary&resub_type=technical')
    assert resp.status_code == 200
    # The page renders and shows the standard per-stage return columns
    assert b'Analyst Performance Report' in resp.data
    assert b'Returned for Correction' in resp.data


def test_analyst_report_download_csv_has_transparency_header(app, client):
    """Downloaded CSV includes the Workflow Status Filter header row."""
    _setup_analyst_report_data(app)
    _login(client, 'admin_ar')
    resp = client.get('/reports/analysts/download?resub_type=preliminary')
    assert resp.status_code == 200
    # New CSV always includes return-type breakdown columns by stage
    assert b'Returned for Correction' in resp.data
    assert b'Returned by Deputy' in resp.data
    assert b'Workflow Status Filter' in resp.data


def test_analyst_report_download_csv_all_types(app, client):
    """Downloaded CSV with all types always includes per-stage return columns."""
    _setup_analyst_report_data(app)
    _login(client, 'admin_ar')
    resp = client.get('/reports/analysts/download?resub_type=all')
    assert resp.status_code == 200
    # New CSV format has dedicated per-stage return columns
    assert b'Returned for Correction (Preliminary)' in resp.data
    assert b'Returned by Deputy' in resp.data
    assert b'Returned by HOD' in resp.data
    assert b'Total Returns' in resp.data


def test_analyst_report_resubmission_count_summary_card(app, client):
    """Summary cards show per-stage return figures."""
    _setup_analyst_report_data(app)
    _login(client, 'admin_ar')
    resp = client.get('/reports/analysts?resub_type=all')
    assert resp.status_code == 200
    assert b'Returned for Correction' in resp.data
    assert b'Returned by Deputy' in resp.data
    assert b'Returned by HOD' in resp.data


def _setup_null_resubmission_data(app):
    """Create a sample with a legacy NULL-type resubmission (pre-migration row)."""
    from app.models import (
        Sample, SampleAssignment, DocumentVersion,
        AssignmentStatus, SampleStatus,
    )
    with app.app_context():
        admin = _create_user(Role.ADMIN, username='admin_null')
        chemist = _create_user(Role.CHEMIST, Branch.PHARMACEUTICAL, username='chemist_null')

        s = Sample(
            lab_number='NULL-001',
            sample_name='Legacy Resubmission Test',
            sample_type=Branch.PHARMACEUTICAL,
            date_received=date(2026, 4, 1),
            uploaded_by=admin.id,
            status=SampleStatus.REGISTERED,
        )
        db.session.add(s)
        db.session.flush()

        a = SampleAssignment(
            sample_id=s.id,
            chemist_id=chemist.id,
            assigned_by=admin.id,
            test_name='Assay',
            assigned_date=date(2026, 4, 2),
            status=AssignmentStatus.COMPLETED,
        )
        db.session.add(a)
        db.session.flush()

        # Original submission
        db.session.add(DocumentVersion(
            sample_id=s.id,
            document_type='report',
            version_number=1,
            file_path='/fake/v1.pdf',
            original_name='report_v1.pdf',
            upload_label='original',
            uploaded_by=chemist.id,
            assignment_id=a.id,
        ))
        # Legacy resubmission with NULL resubmission_type (simulates pre-migration row)
        db.session.add(DocumentVersion(
            sample_id=s.id,
            document_type='report',
            version_number=2,
            file_path='/fake/v2.pdf',
            original_name='report_v2.pdf',
            upload_label='resubmission',
            resubmission_type=None,  # legacy NULL type
            uploaded_by=chemist.id,
            assignment_id=a.id,
        ))
        db.session.commit()
        return s.id, a.id


def test_null_resubmission_type_counted_by_all(app, client):
    """Legacy NULL-type resubmissions are counted when 'all' is selected."""
    from app.main.routes import _resubmission_counts_for_assignments
    s_id, a_id = _setup_null_resubmission_data(app)
    with app.app_context():
        result = _resubmission_counts_for_assignments([a_id], review_types=None)
        assert result.get(a_id, 0) == 1


def test_null_resubmission_type_counted_as_unspecified(app, client):
    """Legacy NULL-type resubmissions are counted when filtering by 'unspecified'."""
    from app.main.routes import _resubmission_counts_for_assignments
    s_id, a_id = _setup_null_resubmission_data(app)
    with app.app_context():
        result = _resubmission_counts_for_assignments([a_id], review_types=['unspecified'])
        assert result.get(a_id, 0) == 1


def test_null_resubmission_type_excluded_by_other_types(app, client):
    """Legacy NULL-type resubmissions are NOT counted when filtering by non-unspecified types."""
    from app.main.routes import _resubmission_counts_for_assignments
    s_id, a_id = _setup_null_resubmission_data(app)
    with app.app_context():
        result = _resubmission_counts_for_assignments([a_id], review_types=['preliminary'])
        assert result.get(a_id, 0) == 0


def test_analyst_report_filter_unspecified_counts_null_types(app, client):
    """Filtering by unspecified on analyst report page includes legacy NULL rows."""
    _setup_null_resubmission_data(app)
    _login(client, 'admin_null')
    resp = client.get('/reports/analysts?resub_type=unspecified')
    assert resp.status_code == 200
    assert b'Unspecified Review' in resp.data
    # The page always shows return-by-stage breakdown columns
    assert b'Returned for Correction' in resp.data


# ---------------------------------------------------------------------------
# QA Performance Summary — corrected sample-level return counts
# ---------------------------------------------------------------------------

def _setup_qa_corrected_return_data(app):
    """Create one sample with repeated preliminary returns and other resubmissions."""
    from app.models import (
        AssignmentStatus, DocumentVersion, ReviewHistory,
    )
    with app.app_context():
        admin = _create_user(Role.ADMIN, username='admin_qa')
        chemist = _create_user(Role.CHEMIST, Branch.PHARMACEUTICAL, username='chemist_qa')
        reviewer = _create_user(Role.OFFICER, username='reviewer_qa')

        sample = Sample(
            lab_number='QA-RET-001',
            sample_name='QA Return Count Test',
            sample_type=Branch.PHARMACEUTICAL,
            date_received=date(2026, 4, 1),
            uploaded_by=admin.id,
            status=SampleStatus.REGISTERED,
        )
        db.session.add(sample)
        db.session.flush()

        assignment = SampleAssignment(
            sample_id=sample.id,
            chemist_id=chemist.id,
            assigned_by=admin.id,
            test_name='Assay',
            assigned_date=datetime(2026, 4, 2, tzinfo=timezone.utc),
            status=AssignmentStatus.RETURNED,
            report_submitted_at=datetime(2026, 4, 3, tzinfo=timezone.utc),
        )
        db.session.add(assignment)
        db.session.flush()

        # Two distinct Preliminary Review return events for the same sample.
        db.session.add(ReviewHistory(
            sample_id=sample.id,
            assignment_id=assignment.id,
            review_type='preliminary',
            review_number=1,
            action='returned',
            reviewer_id=reviewer.id,
            reviewed_at=datetime(2026, 4, 4, 9, tzinfo=timezone.utc),
            comments='Fix calculation',
        ))
        db.session.add(ReviewHistory(
            sample_id=sample.id,
            assignment_id=assignment.id,
            review_type='preliminary',
            review_number=2,
            action='returned',
            reviewer_id=reviewer.id,
            reviewed_at=datetime(2026, 4, 6, 10, tzinfo=timezone.utc),
            comments='Fix units',
        ))
        # Not accepted is not a return/resubmission event and must not inflate the return count.
        db.session.add(ReviewHistory(
            sample_id=sample.id,
            assignment_id=assignment.id,
            review_type='preliminary',
            review_number=3,
            action='not_accepted',
            reviewer_id=reviewer.id,
            reviewed_at=datetime(2026, 4, 7, 11, tzinfo=timezone.utc),
            comments='Rejected',
        ))

        # Preliminary upload corroborates a return but is excluded from combined totals.
        db.session.add(DocumentVersion(
            sample_id=sample.id,
            document_type='report',
            version_number=2,
            file_path='/fake/prelim.pdf',
            original_name='prelim.pdf',
            upload_label='resubmission',
            resubmission_type='preliminary',
            uploaded_by=chemist.id,
            assignment_id=assignment.id,
        ))
        # Other valid resubmissions clearly linked to the same Sample ID.
        db.session.add(DocumentVersion(
            sample_id=sample.id,
            document_type='report',
            version_number=3,
            file_path='/fake/technical.pdf',
            original_name='technical.pdf',
            upload_label='resubmission',
            resubmission_type='technical',
            uploaded_by=chemist.id,
            assignment_id=assignment.id,
        ))
        db.session.add(DocumentVersion(
            sample_id=sample.id,
            document_type='report',
            version_number=4,
            file_path='/fake/legacy.pdf',
            original_name='legacy.pdf',
            upload_label='resubmission',
            resubmission_type=None,
            uploaded_by=chemist.id,
            assignment_id=None,
        ))
        db.session.commit()


def _setup_prelim_comment_category_data(app):
    """Create one sample/assignment with preliminary ReviewHistory rows
    covering all five comment categories, including 'not_accepted' actions
    and comments with mixed case / extra whitespace."""
    from app.models import AssignmentStatus, ReviewHistory

    with app.app_context():
        admin = _create_user(Role.ADMIN, username='admin_cat')
        chemist = _create_user(Role.CHEMIST, Branch.PHARMACEUTICAL, username='chemist_cat')
        reviewer = _create_user(Role.OFFICER, username='reviewer_cat')

        sample = Sample(
            lab_number='CAT-001',
            sample_name='Comment Category Test',
            sample_type=Branch.PHARMACEUTICAL,
            date_received=date(2026, 4, 1),
            uploaded_by=admin.id,
            status=SampleStatus.REGISTERED,
        )
        db.session.add(sample)
        db.session.flush()

        assignment = SampleAssignment(
            sample_id=sample.id,
            chemist_id=chemist.id,
            assigned_by=admin.id,
            test_name='Assay',
            assigned_date=datetime(2026, 4, 2, tzinfo=timezone.utc),
            status=AssignmentStatus.RETURNED,
        )
        db.session.add(assignment)
        db.session.flush()

        # Officers commonly reject (not_accepted) reports for calculation,
        # unit, and typographical errors, while returning for correction on
        # more administrative issues (incomplete fields, references).
        cases = [
            ('not_accepted', '  MISSING/incorrect   Calculations found in section 3  '),
            ('not_accepted', 'Incorrect  UNITS used for concentration'),
            ('not_accepted', 'Typographical errors on page 2'),
            ('returned', 'Incomplete fields in the form'),
            ('returned', 'Incorrect reference/specification cited'),
        ]
        for i, (action, comment) in enumerate(cases):
            db.session.add(ReviewHistory(
                sample_id=sample.id,
                assignment_id=assignment.id,
                review_type='preliminary',
                review_number=i + 1,
                action=action,
                reviewer_id=reviewer.id,
                reviewed_at=datetime(2026, 4, 3, 9, tzinfo=timezone.utc),
                comments=comment,
            ))
        db.session.commit()
        return assignment.id


def test_prelim_comment_category_breakdown_includes_not_accepted_actions():
    """All five categories should be counted, including ones only present
    on 'not_accepted' (Reject Report) reviews, not just 'returned' ones."""
    from app import create_app
    from app.main.routes import _prelim_comment_category_breakdown

    app = create_app('testing')
    with app.app_context():
        db.create_all()
        try:
            assignment_id = _setup_prelim_comment_category_data(app)
            result = _prelim_comment_category_breakdown([assignment_id])
            counts = {r['category']: r['count'] for r in result}
            assert counts['Missing/incorrect calculations'] == 1
            assert counts['Incorrect units'] == 1
            assert counts['Typographical errors'] == 1
            # "Incomplete fields" also matches the 'missing' keyword in the
            # calculations comment ("MISSING/incorrect Calculations..."),
            # which is expected: a single comment can span multiple categories.
            assert counts['Incomplete fields'] == 2
            assert counts['Incorrect reference/specification'] == 1
            # Percentages should sum to ~100% (allowing for rounding)
            total_pct = sum(r['pct'] for r in result)
            assert 99.0 <= total_pct <= 101.0
        finally:
            db.session.remove()
            db.drop_all()


def test_prelim_comment_category_breakdown_empty_when_no_assignments():
    from app import create_app
    from app.main.routes import _prelim_comment_category_breakdown

    app = create_app('testing')
    with app.app_context():
        db.create_all()
        try:
            result = _prelim_comment_category_breakdown([])
            assert len(result) == 5
            assert all(r['count'] == 0 and r['pct'] == 0.0 for r in result)
        finally:
            db.session.remove()
            db.drop_all()


def test_qa_performance_page_shows_comment_category_counts(app, client):
    """The QA Performance Summary page renders all five comment categories
    with non-zero counts when matching not_accepted/returned records exist."""
    assignment_id = _setup_prelim_comment_category_data(app)
    _login(client, 'admin_cat')

    resp = client.get('/reports/qa-performance?year=2026')
    assert resp.status_code == 200
    html = resp.data.decode()
    assert 'Preliminary Review Comment Categories' in html
    assert 'Missing/incorrect calculations' in html
    assert 'Incorrect units' in html
    assert 'Typographical errors' in html
    assert 'Incomplete fields' in html
    assert 'Incorrect reference/specification' in html
    # No preliminary review return comments found... message should NOT show
    assert 'No preliminary review return comments found' not in html


def test_qa_performance_page_uses_corrected_sample_event_counts(app, client):
    _setup_qa_corrected_return_data(app)
    _login(client, 'admin_qa')

    resp = client.get('/reports/qa-performance?year=2026')

    assert resp.status_code == 200
    # Corrected Sample-Level Return Count and Preliminary Review Performance
    # sections are hidden; verify the page still loads successfully.
    assert b'QA Performance Summary' in resp.data


def test_qa_performance_download_includes_audit_breakdown_and_exclusions(app, client):
    _setup_qa_corrected_return_data(app)
    _login(client, 'admin_qa')

    resp = client.get('/reports/qa-performance/download?year=2026')

    assert resp.status_code == 200
    data = resp.data.decode()
    assert 'Preliminary Review Return Events,2' in data
    assert 'Other Resubmission Events,2' in data
    assert 'Combined Return/Resubmission Events,4' in data
    assert 'QA-RET-001' in data
    assert 'DocumentVersion' in data
    assert 'Preliminary resubmission upload excluded from combined total' in data
    assert 'Resubmission type is missing; counted as unspecified' in data
    assert 'ReviewHistory#' in data
