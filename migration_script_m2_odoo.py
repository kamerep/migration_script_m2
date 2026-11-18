#!/usr/bin/env python3
"""M2 Mandat -> CSP Odoo migration: turns the M2 exports into the CSP client import workbook.

    python3 migration_script_m2_odoo.py <folder with the M2 exports> [<output folder>] [--config m2_config.json]
    python3 m2_ui.py                      the same run in a local web UI, with every decision shown live

Reads every .xlsx / .xls / .csv export in the folder (addresses, mandates, officers, beneficial owners,
shareholders, bank accounts, signatories, contact persons, fees, open items), whatever their language
(German or English headers), and writes:

* CSP_Import_M2.xlsx        - the sheets of the csp_client_import template (Clients, Contact Persons,
                              Directors, UBOs, ...): every row in it is expected to load without errors.
* CSP_Import_M2_Review.xlsx - every row left out and why, with the template columns ready to fix and paste,
                              the values adjusted on the way, the M2 values with no CSP column, the column
                              mapping of every file and the full decision journal.
* journal.jsonl / summary.json - the same journal, machine readable (what the UI shows).

Every step is journaled as an event: the file classification (with the score of every candidate), the
header mapping, each value that was cleaned or converted, each link of a register row to its client (and how
it was found), each merge, skip and error, with the rule that decided it and how to fix it. Nothing is ever
put in a default company: a mandate whose operating company cannot be decided waits in the review file.

The template columns come from csp_client_import/models/import_spec.py (CSP_IMPORT_SPEC overrides its path),
so the file always matches the installed module. Needs openpyxl (xlrd for .xls).
"""
import argparse
import collections
import csv
import datetime
import difflib
import importlib.util
import io
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

HERE = Path(__file__).resolve().parent
SPEC_CANDIDATES = [
    os.environ.get('CSP_IMPORT_SPEC', ''),
    '/data/odoo20_enhancements/csp_client_import/models/import_spec.py',
    str(HERE / 'import_spec.py'),
]
IMPORT_FILE = 'CSP_Import_M2.xlsx'
REVIEW_FILE = 'CSP_Import_M2_Review.xlsx'
DEFAULT_CONFIG = HERE / 'm2_config.json'


def load_spec():
    for candidate in SPEC_CANDIDATES:
        if candidate and os.path.isfile(candidate):
            spec = importlib.util.spec_from_file_location('import_spec', candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module, candidate
    raise SystemExit("import_spec.py of csp_client_import not found: set CSP_IMPORT_SPEC to its path.")


import_spec, SPEC_PATH = load_spec()
SHEETS = import_spec.SHEET_BY_KEY
ORDER = [s['key'] for s in import_spec.SHEETS]

# ------------------------------------------------------------------------------------------------------------
# Source kinds: the M2 exports, the header names each field is read from (German and English, compared
# without case, spaces or punctuation) and the words in a file name that point to the kind.
# ------------------------------------------------------------------------------------------------------------

CLIENT_REF = {
    'mandate_no': ['Mandat Nr', 'Mandatsnummer', 'Mandat-Nr', 'MandatNr', 'Mandate No', 'Mandate Number',
                   'Mandate', 'Mandat', 'M-No', 'Mand No', 'M2 Reference', 'Client No', 'Kunden Nr'],
    'debtor_no': ['Debitor Nr', 'Debitorennummer', 'Debitor', 'Debtor No', 'Debtor Number', 'Debtor', 'PK No',
                  'PK Nr', 'Debtor-Adr.No.'],
    'address_no': ['Adress Nr', 'Adressnummer', 'Adr Nr', 'Adrno', 'Adr No', 'Address No', 'Address Number',
                   'ADRNR'],
    'client_name': ['Mandatsname', 'Mandate Name', 'Mandate desig. 1', 'Mandatsbezeichnung 1', 'Firma',
                    'Gesellschaft', 'Company', 'Client', 'Kunde', 'Client Name'],
}
PERSON = {
    'name': ['Name', 'Firma / Name', 'Vollständiger Name', 'Full Name', 'Person'],
    'first_name': ['Vorname', 'Given Name', 'First Name', 'Givenname'],
    'last_name': ['Nachname', 'Familienname', 'Surname', 'Last Name', 'Family Name'],
    'date_of_birth': ['Geburtsdatum', 'Geb Datum', 'Date of Birth', 'Birthdate', 'DOB'],
    'nationality': ['Nationalität', 'Staatsangehörigkeit', 'Nationality', 'Citizenship'],
    'country': ['Land', 'Wohnsitz', 'Wohnland', 'Country', 'Country of Residence', 'Domicile'],
    'address': ['Adresse', 'Anschrift', 'Address', 'Wohnadresse'],
    'remark': ['Bemerkung', 'Bemerkungen', 'Notiz', 'Remark', 'Remark1', 'Remark 1', 'Remarks', 'Notes', 'Note'],
    'remark2': ['Remark2', 'Remark 2', 'Bemerkung 2'],
    'remark3': ['Remark3', 'Remark 3', 'Bemerkung 3'],
}
ADDRESS = {
    'street': ['Strasse', 'Straße', 'Street', 'Adresszeile 1', 'Address Line 1'],
    'street2': ['Adresszusatz', 'Zusatz', 'Postfach', 'Street 2', 'Address Line 2', 'PO Box'],
    'zip_city': ['Postcode, City', 'PLZ Ort', 'PLZ, Ort', 'Postcode City', 'Zip City'],
    'zip': ['PLZ', 'Postleitzahl', 'ZIP', 'Postcode', 'Postal Code'],
    'city': ['Ort', 'Stadt', 'City', 'Town'],
    'country': ['Land', 'Country', 'Country word', 'Country code', 'Landeswort', 'Staat'],
    'phone': ['Telefon', 'Tel', 'Telefon Geschäft', 'Tel Geschäft', 'Phone', 'Phone 1', 'Telephone',
              'Business Phone'],
    'mobile': ['Mobile', 'Natel', 'Handy', 'Mobil', 'Mobile Phone'],
    'email': ['E-Mail', 'Email', 'Mail', 'E Mail Adresse'],
    'website': ['Homepage', 'Internet', 'Website', 'Web'],
}
PERIOD_START = ['Eintritt', 'Eintrittsdatum', 'Gewählt am', 'Von', 'Ab', 'Date of Appointment', 'Appointed',
                'Start', 'Start Date', 'From', 'Date from']
PERIOD_END = ['Austritt', 'Austrittsdatum', 'Bis', 'Date of Resignation', 'Resigned', 'End', 'End Date', 'To',
              'Gelöscht am', 'Date till', 'Date until']
SIGNING = ['Zeichnungsart', 'Zeichnungsberechtigung', 'Unterschrift', 'Signing Authority', 'Signature',
           'Signing']
COLLECTIVE = ['Collective', 'Collectiv', 'Kollektiv', 'Kollektivzeichnung']  # M2 J/N flag: joint signature

KINDS = {
    'mandates': {
        'title': 'Mandates', 'words': ['mandat', 'mandate', 'mandates'],
        'fields': {
            **{k: v for k, v in CLIENT_REF.items() if k != 'client_name'},
            'name': ['Mandatsname', 'Mandate Name', 'Mandate desig. 1', 'Mandate designation 1',
                     'Mandatsbezeichnung 1', 'Firma', 'Firmenname', 'Company Name', 'Name', 'Bezeichnung',
                     'MANDBEZ1'],
            'holder': ['Mandatsträger', 'Mandatstraeger', 'Treuhandmandat', 'Mandate Holder', 'Trust Mandate',
                       'Mad.Company', 'Mand.Company', 'Mandate Company', 'Mandant', 'Holder', 'MADFIRMA'],
            'mandate_kind': ['Mandatsart', 'Mandate Type', 'Art', 'Typ', 'Kategorie', 'Category', 'Code', 'MART'],
            # M2 table export (column names of the database): the number is split in prefix, number, type.
            'mandate_prefix': ['MT'],
            'mandate_seq': ['MNR'],
            'legal_form': ['Rechtsform', 'Gesellschaftsform', 'Legal Form', 'Company Type', 'Mandate form',
                           'Mandatsform', 'Form', 'GESFORM'],
            'jurisdiction': ['Domizil', 'Sitz', 'Sitzland', 'Jurisdiktion', 'Jurisdiction'],
            'contract_start': ['Datum Mandatsvertrag', 'Mandatsbeginn', 'Eröffnung', 'Eröffnet', 'Engagement Letter',
                               'Date of Engagement Letter', 'Engagement Date', 'Opened', 'Opening Date',
                               'Mandate start', 'Mandate start date', 'MANDATS_BEGINN'],
            'contract_end': ['Mandatsende', 'Aufgehoben', 'Saldiert', 'Geschlossen', 'Closed', 'Date Closed',
                             'Closing Date', 'Mandate end', 'Mandate end date', '-end', 'MANDATS_ENDE', 'LOESCHDATUM'],
            'advisor': ['Verantwortlicher', 'Verantwortlich', 'Sachbearbeiter', 'Verantw.SB', 'Verantw SB', 'CDB',
                        'Kundenberater', 'Responsible', 'Responsible Employee', 'Advisor', 'Client Advisor',
                        'VSB'],
            'deputy': ['Stellvertreter', 'Stellv.SB', 'Stellv SB', 'SSB', 'Stv', 'Deputy'],
            'bo_name': ['Wirtschaftlich Berechtigter', 'WB', 'Beneficial Owner', 'BO', 'BO Name', 'UBO'],
            'registration_number': ['Handelsregister Nr', 'HR Nr', 'Handelsregisternummer', 'Registration No',
                                    'Registration Number', 'Company No', 'Firmennummer'],
            'incorporation_date': ['Gründungsdatum', 'Gründung', 'Gegründet', 'Founded', 'Incorporation Date',
                                   'Date of Incorporation'],
            'activity': ['Zweck', 'Gesellschaftszweck', 'Purpose', 'Activity', 'Business activity',
                         'Activity Purpose'],
            'share_capital': ['Aktienkapital', 'Kapital', 'Stammkapital', 'Share Capital', 'Capital'],
            'share_capital_currency': ['Währung Kapital', 'Kapitalwährung', 'Capital Currency', 'KAPWAEHRUNG'],
            'paid_in': ['Einbezahlt', 'Liberiert', 'Paid In', 'Paid Up'],
            'fy_end': ['Geschäftsjahresende', 'Geschäftsjahr Ende', 'Abschlussdatum', 'Financial Year End',
                       'Fiscal Year End', 'FYE'],
            'status': ['Status', 'Zustand'],
            'active': ['Aktiv', 'Active'],
            'vat': ['MWST Nr', 'MWST', 'UID MWST', 'VAT', 'VAT No', 'Tax ID', 'TIN', 'MWSTNR'],
            'lei': ['LEI', 'Legal Entity Identifier'],
            'language': ['Sprache', 'Korrespondenzsprache', 'Language'],
            'notes': ['Bemerkung', 'Bemerkungen', 'Notiz', 'Notes', 'Remark', 'Remarks'],
            'tags': ['Gruppe', 'Mandatsgruppe', 'Group', 'Tags', 'Organisationsform', 'MGRP'],
            **{k: v for k, v in ADDRESS.items()},
        },
        'key_fields': ['mandate_no'],
    },
    'addresses': {
        'title': 'Addresses', 'words': ['adress', 'address', 'adressen', 'addresses', 'kontakte'],
        'fields': {
            'address_no': CLIENT_REF['address_no'] + ['Nr', 'Nummer', 'No', 'Number'],
            'debtor_no': CLIENT_REF['debtor_no'],
            'mandate_no': CLIENT_REF['mandate_no'][:6],
            'name': ['Firma / Name', 'Firma', 'Firmenname', 'Company Name', 'Name', 'Name 1', 'Bezeichnung'],
            'first_name': PERSON['first_name'], 'last_name': PERSON['last_name'],
            'address_kind': ['Adresstyp', 'Adressart', 'Typ', 'Art', 'Type', 'Address Type', 'Kategorie'],
            'language': ['Sprache', 'Korrespondenzsprache', 'Language'],
            'date_of_birth': PERSON['date_of_birth'], 'nationality': PERSON['nationality'],
            'salutation': ['Anrede', 'Salutation', 'Title'],
            'incorporation_date': ['Gründungsdatum', 'Gegründet', 'Founded', 'Date of Incorporation'],
            'activity': ['Zweck', 'Business activity', 'Activity'],
            'notes': PERSON['remark'],
            **ADDRESS,
        },
        'key_fields': ['address_no', 'debtor_no'],
    },
    'contacts': {
        'title': 'Contact persons', 'words': ['kontaktperson', 'ansprechpartner', 'contact person', 'contacts'],
        'fields': {**CLIENT_REF, 'name': PERSON['name'], 'first_name': PERSON['first_name'],
                   'last_name': PERSON['last_name'],
                   'function': ['Funktion', 'Position', 'Job Position', 'Function', 'Role', 'Titel'],
                   'email': ADDRESS['email'], 'phone': ADDRESS['phone'] + ADDRESS['mobile']},
        'key_fields': ['name', 'last_name'],
    },
    'officers': {
        'title': 'Officers (board, secretary, agent)',
        'words': ['organ', 'organe', 'officer', 'verwaltungsrat', 'board', 'zeichnungsberechtigte', 'detail addr',
                  'detailadressen', 'donation advice'],
        'fields': {**CLIENT_REF, **PERSON,
                   'function': ['Funktion', 'Organfunktion', 'Rolle', 'Function', 'Role', 'Position', 'Officer Type',
                                'Type', 'Typ'],
                   'date_start': PERIOD_START, 'date_end': PERIOD_END, 'signing_authority': SIGNING,
                   'collective': COLLECTIVE},
        'key_fields': ['function'],
    },
    'directors': {
        'title': 'Directors', 'words': ['director', 'directors', 'direktor', 'verwaltungsrat', 'vr'],
        'fields': {**CLIENT_REF, **PERSON, 'date_start': PERIOD_START, 'date_end': PERIOD_END,
                   'signing_authority': SIGNING, 'collective': COLLECTIVE, 'function': ['Funktion', 'Function', 'Role']},
        'key_fields': [],
    },
    'ubos': {
        'title': 'Beneficial owners',
        'words': ['ubo', 'beneficial', 'wirtschaftlich', 'berechtigte', 'bo'],
        'fields': {**CLIENT_REF, **PERSON,
                   'date_start': ['Erfasst am', 'Registriert', 'Date of Registry', 'Date Registered'] + PERIOD_START,
                   'date_end': ['Entfernt am', 'Date Removed', 'Removed'] + PERIOD_END,
                   'ownership': ['Anteil', 'Anteil %', 'Beteiligung', 'Beteiligung %', 'Ownership', 'Ownership %',
                                 'Share %', 'Percentage']},
        'key_fields': ['ownership'],
    },
    'shareholders': {
        'title': 'Shareholders', 'words': ['aktionär', 'aktionaer', 'gesellschafter', 'shareholder', 'aktienbuch'],
        'fields': {**CLIENT_REF, **PERSON,
                   'date_start': ['Eintragung', 'Erwerb', 'Date of Entry', 'Date Entered'] + PERIOD_START,
                   'date_end': ['Übertragen', 'Uebertragen', 'Date Transferred', 'Transferred'] + PERIOD_END,
                   'certificate_number': ['Zertifikat Nr', 'Zertifikat', 'Certificate No', 'Certificate'],
                   'share_count': ['Anzahl Aktien', 'Anzahl', 'Aktien', 'No of Shares', 'Shares', 'Number of Shares'],
                   'share_class': ['Aktienart', 'Aktienkategorie', 'Class of Shares', 'Share Class', 'Class']},
        'key_fields': ['share_count', 'certificate_number'],
    },
    'secretaries': {
        'title': 'Secretaries', 'words': ['sekretär', 'sekretaer', 'secretary', 'secretaries'],
        'fields': {**CLIENT_REF, **PERSON, 'date_start': PERIOD_START, 'date_end': PERIOD_END,
                   'signing_authority': SIGNING, 'position': ['Position', 'Funktion', 'Function']},
        'key_fields': [],
    },
    'agents': {
        'title': 'Registered agents', 'words': ['agent', 'agents', 'registered agent', 'domizilhalter'],
        'fields': {**CLIENT_REF, **PERSON, 'date_start': PERIOD_START, 'date_end': PERIOD_END,
                   'signing_authority': SIGNING, 'position': ['Position', 'Funktion', 'Function']},
        'key_fields': [],
    },
    'bank_accounts': {
        'title': 'Bank accounts', 'words': ['bank', 'konto', 'konten', 'account', 'bankverbindung'],
        'fields': {**CLIENT_REF,
                   'acc_number': ['IBAN', 'Konto Nr', 'Kontonummer', 'Konto', 'Account No', 'Account Number',
                                  'Paym.acc.no.', 'Payment account no'],
                   'account_name': ['Kontobezeichnung', 'Bezeichnung', 'Account Name', 'Description', 'Account type',
                                    'Det. type description'],
                   'bank_name': ['Bank', 'Bankname', 'Bank Name', 'Institut', 'Address'],
                   'holder': ['Kontoinhaber', 'Inhaber', 'Account Holder', 'Holder'],
                   'swift_code': ['BIC', 'SWIFT', 'SWIFT Code', 'BIC SWIFT'],
                   'routing_number': ['Clearing', 'Clearing Nr', 'BC Nr', 'BCN', 'Sort Code', 'Routing Number'],
                   'currency': ['Währung', 'Waehrung', 'Currency', 'Whg']},
        'key_fields': ['acc_number'],
    },
    'signatories': {
        'title': 'Bank signatories', 'words': ['signator', 'unterschrift', 'zeichnungsrecht', 'signatories',
                                               'authorized to sign', 'authorised to sign'],
        'fields': {**CLIENT_REF, 'name': PERSON['name'] + ['Address'], 'first_name': PERSON['first_name'],
                   'last_name': PERSON['last_name'],
                   'account': ['IBAN', 'Konto Nr', 'Kontonummer', 'Konto', 'Account No', 'Account Number'],
                   'signature_type': SIGNING + ['Type of Signature Right'],
                   'sign_right': ['Zeichnungsrecht', 'Sign Right', 'Berechtigt'],
                   'collectively': ['Kollektiv', 'Collectively', 'Collectiv', 'Collective', 'Joint']},
        'key_fields': ['signature_type', 'collectively'],
    },
    'fees': {
        'title': 'Fees (ABF / FLF)', 'words': ['honorar', 'fees', 'fee', 'gebühr', 'gebuehr', 'pauschale'],
        'fields': {**{k: v for k, v in CLIENT_REF.items() if k != 'client_name'},
                   'client_name': ['Mandatsname', 'Mandate Name', 'Mandate desig. 1', 'Client Name'],
                   'fee_code': ['Leistungsart', 'Leistung', 'Fee Type', 'H.Type', 'Honorartyp', 'Fee', 'Code', 'Art'],
                   'amount': ['Betrag', 'Amount', 'Honorar', 'Total'],
                   'currency': ['Währung', 'Currency', 'Price currency', 'Preiswährung', 'Whg'],
                   'period': ['Periode', 'Period', 'Intervall', 'Monate', 'Months', 'Deadline'],
                   'next_due': ['Nächste Fälligkeit', 'Fällig', 'Next Due', 'Due Date', 'Fälligkeit', 'Due'],
                   'finish_date': ['Finish date', 'Enddatum', 'Ende', 'End Date'],
                   'billed_by': ['Firma / Name', 'Firma'],
                   'text': ['Text', 'Optionaler Text', 'Optional Text', 'Fee design.', 'Fee designation',
                            'Beschreibung', 'Description']},
        'key_fields': ['fee_code', 'amount'],
    },
    'open_items': {
        'title': 'Open items (receivables)', 'words': ['offene posten', 'open items', 'op liste', 'debitorenliste',
                                                       'receivable', 'offene', 'invoices', 'last invoices',
                                                       'rechnungen'],
        'fields': {**CLIENT_REF,
                   'invoice_no': ['Rechnung Nr', 'Rechnungsnummer', 'Beleg', 'Invoice No', 'OP No', 'OP Nr',
                                  'Invoice'],
                   'invoice_date': ['Rechnungsdatum', 'Datum', 'Invoice Date', 'Date'],
                   'due_date': ['Fällig', 'Fälligkeit', 'Due Date', 'Due'],
                   'amount_open': ['Offen', 'Offener Betrag', 'Saldo', 'Open Amount', 'Open amount foreign',
                                   'Open amount FC', 'Balance', 'Amount Open'],
                   'amount_open_dc': ['Open amount domestic', 'Open amount domes', 'Open amount DC'],
                   'amount': ['Betrag', 'Rechnungsbetrag', 'Amount', 'Invoice Amount', 'Total FC'],
                   'amount_dc': ['Total DC'],
                   'currency': ['Währung', 'Currency', 'Whg'],
                   'paid': ['Paid', 'Bezahlt'],
                   'status': ['Status'],
                   'reminder_level': ['Level of reminder', 'Mahnstufe', 'Reminder level']},
        'key_fields': ['invoice_no', 'amount_open'],
    },
    # M2 Mandate info > Compliance. No CSP template column: carried to the client's Notes and the Compliance
    # sheet of the review file, as M2 has them (the risk rating itself is decided in Odoo, not here).
    'compliance': {
        'title': 'Compliance (ML, risk)', 'words': ['compliance', 'risk', 'risiko', 'gwg'],
        'fields': {**CLIENT_REF,
                   'ml_related': ['ML-Related', 'ML related', 'GwG relevant', 'GwG', 'Money laundering related'],
                   'risk_category': ['Risk category', 'Risikokategorie', 'Risk class', 'Risk'],
                   'risk_country': ['Country of max. risk', 'Country of maximum risk', 'Land mit max. Risiko',
                                    'Risk country'],
                   'lock': ['Lock', 'Locked', 'Sperre', 'Gesperrt'],
                   'lock_text': ['Lock text', 'Lock reason', 'Sperrgrund'],
                   'note': ['Note', 'Notiz', 'Compliance note', 'Bemerkung']},
        'key_fields': ['risk_category', 'ml_related'],
    },
    # M2 Mandate info > Document: the index of the files kept in M2's document store.
    'documents': {
        'title': 'Documents (index)', 'words': ['document', 'documents', 'dokument', 'dokumente', 'ablage'],
        'fields': {**CLIENT_REF,
                   'file_name': ['File name', 'Dateiname', 'Filename', 'Datei'],
                   'folder': ['Folder', 'Ordner', 'Path', 'Pfad', 'Directory'],
                   'description': ['Description', 'Beschreibung'],
                   'created': ['Creation date', 'Erstellt', 'Erstellungsdatum', 'Created'],
                   'extension': ['Extension', 'Endung'],
                   'doc_type': ['Type', 'Typ', 'Dokumenttyp'],
                   'maturity': ['Maturity date', 'Fälligkeit', 'Ablaufdatum', 'Expiry'],
                   'creator': ['Creator', 'Ersteller']},
        'key_fields': ['file_name'],
    },
    # A file list of the M2 document archive (e.g. Z:\\NOVA-PH\\KP-00031-ENT\\KYC\\pass.pdf), made on the Windows
    # side with Get-ChildItem ... | Export-Csv, when the archive itself is not copied. The mandate is the first
    # folder of the path named like a mandate number; the folder above it is the mandate holder's archive.
    'inventory': {
        'title': 'Archive inventory (file list)', 'words': ['inventory', 'inventar', 'dateiliste', 'filelist',
                                                             'file list', 'archiv'],
        'fields': {'path': ['Path', 'FullName', 'Full Name', 'Pfad', 'Relative Path', 'File path'],
                   'size': ['Length', 'Size', 'Bytes', 'Grösse', 'Größe', 'MB'],
                   'modified': ['Modified', 'LastWriteTime', 'Last Write Time', 'Date modified', 'Geändert',
                                'Änderungsdatum']},
        'key_fields': ['path'],
    },
}

# M2 'Mad.Company' (the managing company of the record) on the register grids: kept apart so that it is never
# read as the client's name.
for _kind, _spec in KINDS.items():
    if _kind not in ('mandates', 'inventory'):
        _spec['fields']['managing_company'] = ['Mad.Company', 'Mand.Company', 'Managing company']

# Officer function (normalized) -> template sheet, for a combined officers export
OFFICER_FUNCTIONS = [
    (r'sekret|secretar', 'secretaries'),
    (r'agent|domizil', 'agents'),
    (r'wirtschaftlich|beneficial|\bubo\b|\bbo\b', 'ubos'),
    (r'aktionär|aktionaer|shareholder|gesellschafter', 'shareholders'),
    (r'kontakt|contact|ansprech', 'contacts'),
    (r'verwaltungsrat|\bvr\b|director|direktor|präsident|praesident|president|mitglied|member|'
     r'geschäftsführ|geschaeftsfuehr|manager|board|stiftungsrat|council|protector|trustee|treuhänder|'
     r'liquidator|prokur|zeichnungsberechtigt|signatory', 'directors'),
]

# M2 legal-form codes (manual: AG, GMBH, LLP, STI, TRU, VTR) and common forms -> csp_entity_type
LEGAL_FORMS = {
    'company': ['ag', 'gmbh', 'llp', 'ltd', 'limited', 'inc', 'corp', 'corporation', 'llc', 'sa', 'sarl', 'ibc',
                'plc', 'bv', 'nv', 'kg', 'gesellschaft', 'company', 'fzco', 'fze', 'fz llc', 'pte ltd', 'est',
                'anstalt', 'establishment'],
    'foundation': ['sti', 'stiftung', 'foundation', 'fondation', 'private foundation', 'pif'],
    'trust': ['tru', 'trust', 'vtr', 'vista trust', 'treuhand', 'trusteeship'],
}
INDIVIDUAL_KINDS = {'kli', 'klient', 'client', 'person', 'privat', 'privatperson', 'individual', 'natürliche person',
                    'natuerliche person', 'np', 'p', 'con', 'dir'}
COMPANY_KINDS = {'ges', 'gesellschaft', 'firma', 'company', 'corporate', 'juristische person', 'jp', 'f', 'ent',
                 'acc', 'adm'}
CLOSED_STATUS = {'inaktiv', 'inactive', 'closed', 'geschlossen', 'saldiert', 'aufgehoben', 'gelöscht', 'geloescht',
                 'deleted', 'liquidiert', 'liquidated', 'struck off', 'dissolved', '0', 'false', 'nein', 'no'}

ISO2 = set(
    'AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ '
    'CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO '
    'FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE '
    'JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO '
    'MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW '
    'PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM '
    'TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS XK YE YT ZA ZM ZW'.split())
COUNTRY_NAMES = {
    'CH': 'Schweiz Suisse Svizzera Switzerland Swiss', 'LI': 'Liechtenstein FL', 'DE': 'Deutschland Germany Allemagne',
    'AT': 'Österreich Oesterreich Austria Autriche', 'FR': 'Frankreich France', 'IT': 'Italien Italy Italie Italia',
    'GB': 'Grossbritannien Großbritannien Vereinigtes Königreich United Kingdom UK England Great Britain Scotland Wales',
    'US': 'USA Vereinigte Staaten United States United States of America', 'AE': 'VAE UAE Vereinigte Arabische '
    'Emirate United Arab Emirates Dubai Abu Dhabi', 'HK': 'Hongkong Hong Kong', 'SG': 'Singapur Singapore',
    'CN': 'China Volksrepublik China PRC', 'VG': 'BVI British Virgin Islands Britische Jungferninseln Tortola',
    'KY': 'Cayman Islands Kaimaninseln Cayman', 'PA': 'Panama', 'BZ': 'Belize', 'MH': 'Marshall Islands '
    'Marshallinseln', 'SC': 'Seychellen Seychelles', 'CY': 'Zypern Cyprus', 'MT': 'Malta', 'LU': 'Luxemburg '
    'Luxembourg', 'NL': 'Niederlande Netherlands Holland', 'BE': 'Belgien Belgium', 'ES': 'Spanien Spain',
    'PT': 'Portugal', 'IE': 'Irland Ireland', 'GI': 'Gibraltar', 'JE': 'Jersey', 'GG': 'Guernsey', 'IM': 'Isle of Man',
    'BS': 'Bahamas', 'BM': 'Bermuda', 'MU': 'Mauritius', 'RU': 'Russland Russia Russian Federation', 'UA': 'Ukraine',
    'PL': 'Polen Poland', 'CZ': 'Tschechien Czech Republic Czechia', 'HU': 'Ungarn Hungary', 'GR': 'Griechenland Greece',
    'TR': 'Türkei Tuerkei Turkey Türkiye', 'IL': 'Israel', 'SA': 'Saudi-Arabien Saudi Arabia', 'QA': 'Katar Qatar',
    'KW': 'Kuwait', 'BH': 'Bahrain', 'OM': 'Oman', 'IN': 'Indien India', 'PK': 'Pakistan', 'PH': 'Philippinen '
    'Philippines', 'MY': 'Malaysia', 'ID': 'Indonesien Indonesia', 'TH': 'Thailand', 'VN': 'Vietnam', 'JP': 'Japan',
    'KR': 'Südkorea Suedkorea South Korea Korea', 'TW': 'Taiwan', 'AU': 'Australien Australia', 'NZ': 'Neuseeland '
    'New Zealand', 'CA': 'Kanada Canada', 'MX': 'Mexiko Mexico', 'BR': 'Brasilien Brazil', 'AR': 'Argentinien '
    'Argentina', 'CL': 'Chile', 'CO': 'Kolumbien Colombia', 'ZA': 'Südafrika Suedafrika South Africa', 'EG': 'Ägypten '
    'Aegypten Egypt', 'NG': 'Nigeria', 'KE': 'Kenia Kenya', 'MC': 'Monaco', 'SE': 'Schweden Sweden', 'NO': 'Norwegen '
    'Norway', 'DK': 'Dänemark Daenemark Denmark', 'FI': 'Finnland Finland', 'SK': 'Slowakei Slovakia', 'SI': 'Slowenien '
    'Slovenia', 'HR': 'Kroatien Croatia', 'RS': 'Serbien Serbia', 'RO': 'Rumänien Rumaenien Romania', 'BG': 'Bulgarien '
    'Bulgaria', 'LB': 'Libanon Lebanon', 'JO': 'Jordanien Jordan', 'IR': 'Iran', 'IQ': 'Irak Iraq', 'KZ': 'Kasachstan '
    'Kazakhstan', 'AZ': 'Aserbaidschan Azerbaijan', 'GE': 'Georgien Georgia', 'AM': 'Armenien Armenia',
    'CW': 'Curaçao Curacao', 'AI': 'Anguilla', 'TC': 'Turks and Caicos Islands', 'VC': 'St. Vincent '
    'Saint Vincent and the Grenadines', 'KN': 'St. Kitts and Nevis Saint Kitts and Nevis Nevis', 'LC': 'St. Lucia '
    'Saint Lucia', 'AG': 'Antigua and Barbuda Antigua', 'WS': 'Samoa', 'VU': 'Vanuatu', 'CK': 'Cook Islands',
    'LR': 'Liberia', 'DM': 'Dominica', 'GD': 'Grenada', 'BB': 'Barbados', 'UY': 'Uruguay', 'CR': 'Costa Rica',
}
COUNTRY_MULTI = {'Saint Vincent and the Grenadines', 'United States of America', 'United Arab Emirates', 'Hong Kong',
                 'British Virgin Islands', 'Cayman Islands', 'Marshall Islands', 'Isle of Man', 'Turks and Caicos '
                 'Islands', 'Saint Kitts and Nevis', 'Antigua and Barbuda', 'Cook Islands', 'South Africa',
                 'New Zealand', 'Czech Republic', 'Saudi Arabia', 'Russian Federation', 'South Korea',
                 'Great Britain', 'United Kingdom', 'United States', 'Vereinigtes Königreich',
                 'Vereinigte Staaten', 'Vereinigte Arabische Emirate', 'Volksrepublik China', 'Abu Dhabi',
                 'Britische Jungferninseln', 'Saint Lucia', 'St. Lucia', 'St. Vincent', 'St. Kitts and Nevis',
                 'Costa Rica', 'Private Foundation'}
ISO3 = {'CHE': 'CH', 'LIE': 'LI', 'DEU': 'DE', 'AUT': 'AT', 'GBR': 'GB', 'USA': 'US', 'ARE': 'AE', 'HKG': 'HK',
        'SGP': 'SG', 'CHN': 'CN', 'VGB': 'VG', 'CYM': 'KY', 'PAN': 'PA', 'BLZ': 'BZ', 'MHL': 'MH', 'SYC': 'SC',
        'FRA': 'FR', 'ITA': 'IT', 'RUS': 'RU', 'IND': 'IN', 'PHL': 'PH', 'LUX': 'LU', 'NLD': 'NL', 'CYP': 'CY',
        'MLT': 'MT', 'ESP': 'ES', 'BHS': 'BS', 'MUS': 'MU'}
COUNTRY_INDEX = {}
for _code, _names in COUNTRY_NAMES.items():
    _rest = _names
    for _multi in COUNTRY_MULTI:
        if _multi in _rest:
            COUNTRY_INDEX[' '.join(_multi.casefold().split())] = _code
            _rest = _rest.replace(_multi, ' ')
    for _word in _rest.split():
        COUNTRY_INDEX[_word.casefold()] = _code

CURRENCIES = {'CHF', 'EUR', 'USD', 'GBP', 'AED', 'HKD', 'SGD', 'CNY', 'JPY', 'CAD', 'AUD', 'NZD', 'SEK', 'NOK',
              'DKK', 'PLN', 'CZK', 'HUF', 'RUB', 'INR', 'PHP', 'SAR', 'QAR', 'KWD', 'BHD', 'OMR', 'ZAR', 'TRY',
              'ILS', 'MXN', 'BRL', 'MYR', 'THB', 'IDR', 'KRW', 'TWD'}
CURRENCY_WORDS = {'fr': 'CHF', 'sfr': 'CHF', 'franken': 'CHF', '€': 'EUR', 'euro': 'EUR', 'euros': 'EUR',
                  '$': 'USD', 'us$': 'USD', 'usd$': 'USD', 'dollar': 'USD', '£': 'GBP', 'pound': 'GBP', 'rmb': 'CNY'}
SIGNING_MAP = [(r'einzel|sole|single|singly|alone|allein', 'singly'),
               (r'kollektiv|joint|zu zweien|two|collective|jointly|gemeinsam', 'jointly')]
YES = {'1', 'true', 'yes', 'ja', 'x', 'y', 'j', 'oui', 'wahr'}
NO = {'0', 'false', 'no', 'nein', 'n', 'non', 'falsch', ''}
NOT_APPLICABLE = {'n/a', 'na', 'not applicable', 'none', '-', '--', '/', 'not known', 'unbekannt', 'k.a.', 'ka',
                  'keine', 'tbd', '?', 'nil', 'null'}
START_FIELD = {'directors': 'date_appointment', 'ubos': 'date_registry', 'shareholders': 'date_entry',
               'secretaries': 'date_appointment', 'agents': 'date_appointment'}
END_FIELD = {'directors': 'date_resignation', 'ubos': 'date_removed', 'shareholders': 'date_transferred',
             'secretaries': 'date_resignation', 'agents': 'date_resignation'}
REGISTER_KINDS = ['contacts', 'directors', 'ubos', 'shareholders', 'secretaries', 'agents', 'bank_accounts',
                  'signatories']

COMPANY_WORDS = re.compile(r'\b(ag|gmbh|ltd|limited|inc|llc|llp|sa|sarl|trust|stiftung|foundation|corp|plc|fzco|fze|'
                           r'bv|nv|kg|holding|treuhand|anstalt|establishment|company|co|partners|group)\b\.?', re.I)
# An M2 mandate number in a file name or report title: KP-482-ACC, KP-00482-ACC, M-0001
MANDATE_RE = re.compile(r'(?<![A-Za-z0-9])([A-Z]{1,5}-\d{1,6}(?:-[A-Z0-9]{1,6})?)(?![A-Za-z0-9])')
LEVELS = ('error', 'review', 'warning', 'decision', 'change', 'not_loaded', 'info')


# ------------------------------------------------------------------------------------------------------------
# Small helpers
# ------------------------------------------------------------------------------------------------------------

def norm(value):
    """Header / lookup key: lower case, accents kept, punctuation and spacing ignored."""
    text_ = str(value or '').strip().casefold().replace('ß', 'ss')
    text_ = re.sub(r'[\s_.\-:/#()*]+', ' ', text_)
    return ' '.join(text_.split())


def rkey(value):
    """Lookup key of an M2 number (mandate, debtor, address): as norm(), with the zero padding of each digit group
    dropped, so KP-482-ACC (mandate list) and KP-00482-ACC (document folder) or 021091 and 21091 are one key."""
    return re.sub(r'(?<![\d])0+(?=\d)', '', norm(value))


def text(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.strftime('%Y-%m-%d')
    return ' '.join(str(value).replace(' ', ' ').split())


def jsonable(value):
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return text(value)


def short(value, size=120):
    value = text(value)
    return value if len(value) <= size else value[:size - 1] + '…'


class Journal:
    """Every event of a run, in order. `listener` receives each one as it is written (the UI streams them)."""

    def __init__(self, path=None, listener=None):
        self.events = []
        self.counts = collections.Counter()
        self.path = path
        self.listener = listener
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, 'w', encoding='utf-8') if path else None
        self.stage = 'start'
        self.t0 = time.time()

    def add(self, level, code, message, **data):
        event = {'seq': len(self.events) + 1, 't': round(time.time() - self.t0, 3), 'level': level, 'code': code,
                 'stage': self.stage, 'message': message}
        for k, v in data.items():
            if v not in (None, '', [], {}):
                event[k] = [jsonable(x) for x in v] if isinstance(v, list) else jsonable(v) \
                    if not isinstance(v, dict) else {a: jsonable(b) for a, b in v.items()}
        self.events.append(event)
        self.counts[level] += 1
        if self._fh:
            self._fh.write(json.dumps(event, ensure_ascii=False) + '\n')
            self._fh.flush()
        if self.listener:
            self.listener(event)
        return event

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None


# ------------------------------------------------------------------------------------------------------------
# Value conversion. Each returns (value, note): note explains a change, value None = not usable (with reason)
# ------------------------------------------------------------------------------------------------------------

class Convert:

    def __init__(self, config):
        self.date_order = (config.get('date_order') or 'DMY').upper()

    def date(self, raw):
        if raw in (None, ''):
            return None, None
        if isinstance(raw, datetime.datetime):
            return raw.date(), None
        if isinstance(raw, datetime.date):
            return raw, None
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            if 20000 < raw < 80000:  # Excel serial day number
                value = datetime.date(1899, 12, 30) + datetime.timedelta(days=int(raw))
                return value, f"Excel day number {text(raw)} read as {value:%Y-%m-%d}."
            return None, f"'{text(raw)}' is a number, not a date."
        value = text(raw)
        if value.casefold() in NOT_APPLICABLE:
            return None, f"'{value}' means no date: left empty."
        iso = re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T].*)?', value)
        if iso:
            return self._make(int(iso[1]), int(iso[2]), int(iso[3]), value, None)
        parts = re.fullmatch(r'(\d{1,2})[./\-](\d{1,2})[./\-](\d{2}|\d{4})(?:\s.*)?', value)
        if parts:
            a, b, year = int(parts[1]), int(parts[2]), int(parts[3])
            note = None
            if year < 100:
                pivot = datetime.date.today().year % 100 + 1
                year += 2000 if year < pivot else 1900
                note = f"Two-digit year '{parts[3]}' read as {year}."
            sep = value[len(parts[1])]
            if sep == '.' or self.date_order == 'DMY':
                day, month = a, b
            else:
                month, day = a, b
            if month > 12 and day <= 12:
                day, month = month, day
                note = (note + ' ' if note else '') + f"'{value}' only makes sense as month/day: day and month swapped."
            elif a <= 12 and b <= 12 and a != b and sep != '.':
                note = (note + ' ' if note else '') + (
                    f"'{value}' could be day/month or month/day: read as {self.date_order} (Settings > date order).")
            return self._make(year, month, day, value, note)
        for fmt in ('%d %b %Y', '%d %B %Y', '%b %d %Y', '%B %d %Y', '%d-%b-%Y', '%d-%b-%y', '%Y%m%d'):
            try:
                return datetime.datetime.strptime(value.replace(',', ''), fmt).date(), None
            except ValueError:
                pass
        if re.fullmatch(r'\d{1,2}[./]\d{1,2}\.?', value) or re.fullmatch(r'\d{1,2}[./]\d{4}', value):
            return None, f"'{value}' is not a full date (no day or no year)."
        return None, f"'{value}' is not a date in any known format."

    @staticmethod
    def _make(year, month, day, raw, note):
        try:
            return datetime.date(year, month, day), note
        except ValueError:
            return None, f"'{raw}' is not a valid date (day {day}, month {month})."

    @staticmethod
    def number(raw):
        """1'000.50 / 1.000,50 / 1,000.50 / CHF 100'000 -> (float, currency or None, note)."""
        if raw in (None, ''):
            return None, None, None
        if isinstance(raw, bool):
            return None, None, "A yes/no value, not an amount."
        if isinstance(raw, (int, float)):
            return float(raw), None, None
        value = text(raw)
        if value.casefold() in NOT_APPLICABLE:
            return None, None, f"'{value}' means no amount: left empty."
        currency = None
        for word in re.findall(r"[A-Za-z€$£]+\$?", value):
            code = word.upper() if word.upper() in CURRENCIES else CURRENCY_WORDS.get(word.casefold())
            if code:
                currency = code
        digits = re.sub(r"[^\d'.,\-]", '', value).replace("'", '').replace('’', '')
        if not digits or not re.search(r'\d', digits):
            return None, currency, f"'{value}' holds no amount."
        if ',' in digits and '.' in digits:
            if digits.rfind(',') > digits.rfind('.'):
                digits = digits.replace('.', '').replace(',', '.')
            else:
                digits = digits.replace(',', '')
        elif ',' in digits:
            head, _sep, tail = digits.rpartition(',')
            digits = digits.replace(',', '.') if len(tail) in (1, 2) and digits.count(',') == 1 else \
                digits.replace(',', '')
        elif digits.count('.') > 1:
            digits = digits.replace('.', '')
        try:
            amount = float(digits)
        except ValueError:
            return None, currency, f"'{value}' is not readable as one amount."
        note = None if text(amount) == value else f"'{value}' read as {amount:,.2f}" + (
            f" {currency}." if currency else ".")
        return amount, currency, note

    @staticmethod
    def country(raw):
        value = text(raw)
        if not value or value.casefold() in NOT_APPLICABLE:
            return None, None
        upper = value.upper().strip('.')
        if len(upper) == 2 and upper in ISO2:
            return upper, None
        if upper == 'UK':
            return 'GB', "'UK' read as GB."
        if upper in ISO3:
            return ISO3[upper], f"ISO3 code '{value}' read as {ISO3[upper]}."
        key = ' '.join(value.casefold().replace('-', ' ').split())
        if key in COUNTRY_INDEX:
            code = COUNTRY_INDEX[key]
            return code, None if key == code.casefold() else f"'{value}' read as {code}."
        return None, f"'{value}' is not a country code or a known country name."

    @staticmethod
    def currency(raw):
        value = text(raw)
        if not value:
            return None, None
        if value.upper() in CURRENCIES:
            return value.upper(), None
        code = CURRENCY_WORDS.get(value.casefold())
        if code:
            return code, f"'{value}' read as {code}."
        return None, f"'{value}' is not an ISO currency code."

    @staticmethod
    def boolean(raw):
        value = text(raw).casefold()
        if value in YES:
            return 'Yes', None
        if value in NO:
            return ('No', None) if value else (None, None)
        return None, f"'{text(raw)}' is not a yes / no value."

    @staticmethod
    def email(raw):
        value = text(raw)
        if not value:
            return None, None
        found = re.findall(r'[\w.+\-\']+@[\w\-]+(?:\.[\w\-]+)+', value)
        if not found:
            return None, f"'{value}' is not an email address."
        if len(found) > 1:
            return found[0], f"Several addresses: {found[0]} is kept; not loaded: {', '.join(found[1:])}."
        if found[0] != value:
            return found[0], f"Only the address is kept from '{value}'."
        return value, None

    @staticmethod
    def signing(raw):
        value = text(raw)
        if not value:
            return None, None
        for pattern, code in SIGNING_MAP:
            if re.search(pattern, value, re.I):
                return code, None if value.casefold() == code else f"'{value}' read as {code}."
        return None, f"'{value}' is neither sole nor joint signature."


# ------------------------------------------------------------------------------------------------------------
# Reading the exports
# ------------------------------------------------------------------------------------------------------------

def read_csv_bytes(content):
    for encoding in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            data = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    sample = data[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=';,\t|')
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ';' if sample.count(';') > sample.count(',') else ','
    rows = [row for row in csv.reader(io.StringIO(data), delimiter=delimiter)]
    return rows, encoding, delimiter


def read_tables(path):
    """[(sheet name, rows, note)] of one export file."""
    name = path.name.lower()
    if name.endswith(('.csv', '.txt')):
        rows, encoding, delimiter = read_csv_bytes(path.read_bytes())
        return [(path.stem, rows, f"CSV, encoding {encoding}, delimiter '{delimiter}'")]
    if name.endswith('.xls'):
        import xlrd  # noqa: PLC0415
        book = xlrd.open_workbook(str(path))
        tables = []
        for ws in book.sheets():
            rows = []
            for r in range(ws.nrows):
                row = []
                for cell in ws.row(r):
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        row.append(xlrd.xldate.xldate_as_datetime(cell.value, book.datemode))
                    else:
                        row.append(cell.value)
                rows.append(row)
            tables.append((ws.name, rows, 'Excel 97-2003 (.xls)'))
        return tables
    book = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    tables = [(ws.title, [list(r) for r in ws.iter_rows(values_only=True)], 'Excel (.xlsx)') for ws in book.worksheets]
    book.close()
    return tables


def find_header(rows, limit=15):
    """Index of the header row: M2 report exports often start with a title and print date."""
    best, best_score = 0, -1
    for i, row in enumerate(rows[:limit]):
        cells = [text(c) for c in row]
        filled = [c for c in cells if c]
        if not filled:
            continue
        textual = sum(1 for c in filled if not re.fullmatch(r'[\d\s.,:/\-\']+', c))
        score = textual * 2 + len(filled) - (5 if len(filled) == 1 else 0)
        if score > best_score:
            best, best_score = i, score
    return best


# ------------------------------------------------------------------------------------------------------------
# The migration
# ------------------------------------------------------------------------------------------------------------

class Migration:

    def __init__(self, source, out_folder, config=None, listener=None, journal_path=None):
        self.source = Path(source)
        self.out = Path(out_folder)
        self.config = config or {}
        self.journal = Journal(journal_path, listener)
        self.conv = Convert(self.config)
        self.kinds = self._kinds()
        self.tables = collections.defaultdict(list)     # kind -> [table dict]
        self.files = []                                  # per table: file, sheet, kind, scores, mapping, unused
        self.rows = {k: [] for k in ORDER}               # rows of the import file
        self.review = {k: [] for k in ORDER}             # (where, m2 key, reason, fix, row)
        self.changes = []
        self.not_loaded = []
        self.closed = []
        self.receivables = collections.defaultdict(lambda: collections.defaultdict(float))
        self.fee_rows = []
        self.records = collections.defaultdict(list)     # m2 key -> event seqs (the decision trail)
        self.clients = {}                                # m2 reference -> client row
        self.lookup = {'mandate': {}, 'debtor': {}, 'address': {}, 'name': collections.defaultdict(list)}
        self.excluded = {rkey(k) for k in self.config.get('excluded_keys') or []}
        self.pending_bo = []                             # (where, mandate, BO name) named on a mandate
        self.bank_relations = collections.defaultdict(list)  # client -> M2 bank relationships (no account no)
        self.compliance = []                             # M2 compliance data per client (no template column)
        self.documents = []                              # index of M2 documents per client

    # -- journal ---------------------------------------------------------------------------------------------

    def ev(self, level, code, message, key=None, **data):
        event = self.journal.add(level, code, message, key=key, **data)
        if key:
            self.records[str(key)].append(event['seq'])
        return event

    def stage(self, name, message):
        self.journal.stage = name
        self.ev('info', 'STAGE', message)

    def _kinds(self):
        kinds = json.loads(json.dumps(KINDS))
        for kind, extra in (self.config.get('extra_aliases') or {}).items():
            if kind in kinds and isinstance(extra, dict):
                for field, headers in extra.items():
                    kinds[kind]['fields'].setdefault(field, [])
                    kinds[kind]['fields'][field] = list(headers) + kinds[kind]['fields'][field]
        return kinds

    # -- 1. files --------------------------------------------------------------------------------------------

    def discover(self):
        self.stage('files', f"Reading the exports in {self.source}")
        if not self.source.is_dir():
            self.ev('error', 'NO_SOURCE', f"Folder {self.source} does not exist.", fix="Choose the folder with the M2 "
                    "exports.")
            return
        paths = sorted(p for p in self.source.iterdir() if p.is_file() and not p.name.startswith(('~$', '.'))
                       and p.suffix.lower() in ('.xlsx', '.xlsm', '.xls', '.csv', '.txt')
                       and p.name.lower() != 'archive_holders.txt')        # the holder list, not an export
        skipped = sorted(p.name for p in self.source.iterdir() if p.is_file() and p not in paths)
        for name in skipped:
            self.ev('info', 'FILE_SKIPPED', f"{name}: not a spreadsheet export, not read.", file=name)
        if not paths:
            self.ev('error', 'NO_FILES', "No .xlsx, .xls or .csv export in the folder.")
        overrides = {norm(k): v for k, v in (self.config.get('file_kinds') or {}).items() if not k.startswith('_')}
        for path in paths:
            if path.name in (IMPORT_FILE, REVIEW_FILE):
                continue
            try:
                tables = read_tables(path)
            except Exception as exc:  # noqa: BLE001 - every unreadable file is reported, the run goes on
                self.ev('error', 'FILE_UNREADABLE', f"{path.name} cannot be read: {exc}", file=path.name,
                        fix="Save it again from Excel as .xlsx, or as CSV (UTF-8).")
                continue
            for sheet, rows, note in tables:
                self.classify(path, sheet, rows, note, overrides)

    def classify(self, path, sheet, rows, note, overrides):
        label = path.name if len(rows) and sheet in (path.stem, 'Sheet1', 'Tabelle1', 'Sheet') else \
            f"{path.name} [{sheet}]"
        rows = [r for r in rows if r is not None]
        if not any(any(text(c) for c in r) for r in rows):
            self.ev('info', 'SHEET_EMPTY', f"{label}: empty sheet.", file=label)
            return
        h = find_header(rows)
        header = [text(c) for c in rows[h]]
        if h:
            self.ev('decision', 'HEADER_ROW', f"{label}: header found on row {h + 1}; rows above it "
                    f"({'; '.join(short(' '.join(text(c) for c in r if text(c)), 60) for r in rows[:h])}) are a "
                    "report title, not data.", file=label, row=h + 1)
        normalized = [norm(c) for c in header]
        scores = {}
        for kind, spec in self.kinds.items():
            matched = self._match_headers(normalized, spec['fields'])
            score = len(matched) + 2 * sum(1 for f in spec['key_fields'] if f in matched)
            words = norm(f"{path.stem} {sheet}")
            if any(re.search(rf'(?<![a-z]){re.escape(w)}', words) for w in spec['words']):
                score += 4
            scores[kind] = score
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        override = next((v for k, v in overrides.items() if norm(path.name).startswith(k) or norm(label) == k), None)
        if override:
            kind = override
            self.ev('decision', 'KIND_OVERRIDE', f"{label}: read as {kind} (Settings > file kinds).", file=label,
                    scores=dict(ranked[:5]))
        else:
            kind, top = ranked[0]
            second = ranked[1][1] if len(ranked) > 1 else 0
            if top < 4:
                self.ev('error', 'KIND_UNKNOWN', f"{label}: not recognised as any M2 export (best guess "
                        f"{kind}, score {top}). Not read.", file=label, scores=dict(ranked[:5]), header=header[:40],
                        fix="Add the file to Settings > file kinds with its kind, or add its headers to the aliases.")
                return
            level = 'warning' if top - second <= 1 else 'decision'
            self.ev(level, 'KIND', f"{label}: read as {self.kinds[kind]['title']} (score {top}"
                    + (f"; next {ranked[1][0]} {second}" if len(ranked) > 1 else '') + ")."
                    + (" Close call: check it, or fix it in Settings > file kinds." if level == 'warning' else ''),
                    file=label, scores=dict(ranked[:5]))
        if kind == 'ignore':
            self.ev('info', 'FILE_IGNORED', f"{label}: ignored (Settings > file kinds).", file=label)
            return
        if kind not in self.kinds:
            self.ev('error', 'KIND_INVALID', f"{label}: kind '{kind}' in Settings does not exist.", file=label)
            return
        filled = {i for r in rows[h + 1:] for i, c in enumerate(r) if text(c)}
        mapping = self._match_headers(normalized, self.kinds[kind]['fields'], filled)
        index = {field: pos for field, (pos, _alias) in mapping.items()}
        for field, (pos, alias) in sorted(mapping.items(), key=lambda kv: kv[1][0]):
            self.ev('info', 'COLUMN', f"{label}: column '{header[pos]}' -> {field}"
                    + (f" (alias '{alias}')" if norm(alias) != norm(header[pos]) else ''), file=label, column=header[pos],
                    field=field)
        # An export of one mandate's grid (Mandate info > Bank, Fees, Detail addr...) has no mandate column:
        # the mandate number is then read from the file / sheet name or the report title above the header.
        context_ref = None
        # (An address number alone does not name the client there: on those grids it is the person's or the
        # bank's own address.)
        if kind not in ('mandates', 'addresses', 'inventory') and not any(f in index for f in ('mandate_no',
                                                                                               'debtor_no')):
            title = ' '.join(text(c) for r in rows[:h] for c in r if text(c))
            found = MANDATE_RE.search(f"{path.stem} {sheet}")
            source = 'file name'
            if not found:
                found, source = MANDATE_RE.search(title), 'report title'
            if found:
                context_ref = found[1]
                self.ev('decision', 'REF_FROM_FILE', f"{label}: no mandate column; every row belongs to mandate "
                        f"{context_ref}, read from the {source}."
                        + (f" Column '{header[index['address_no']]}' is the address of the person / bank, not of "
                           "the client." if 'address_no' in index else ''), file=label, key=context_ref)
            else:
                self.ev('warning', 'NO_CLIENT_COLUMN', f"{label}: no mandate, debtor or address number column and "
                        "none in the file name: its rows cannot be linked to a client.", file=label,
                        fix="Export with the mandate column, or put the mandate number in the file name "
                        "(e.g. 'KP-482-ACC Bank.xlsx').")
        used = set(index.values())
        unused = []
        for pos, name in enumerate(header):
            if pos in used or not name:
                continue
            samples = [short(r[pos], 40) for r in rows[h + 1:h + 200] if pos < len(r) and text(r[pos])][:3]
            unused.append({'column': name, 'samples': samples})
            if samples:
                self.ev('not_loaded', 'COLUMN_UNUSED', f"{label}: column '{name}' has no CSP field; its values are "
                        "kept in the review file (Not Loaded)" + (" and in the client's Notes." if
                                                                   self.config.get('notes_from_unmapped', True) and
                                                                   kind == 'mandates' else '.'),
                        file=label, column=name, samples=samples,
                        fix=f"If it holds a CSP field, add '{name}' to Settings > extra aliases for {kind}.")
        data = []
        blank = parts = 0
        for number_, raw in enumerate(rows[h + 1:], start=h + 2):
            if not any(text(c) for c in raw):
                blank += 1
                continue
            if self._is_total(raw):
                self.ev('decision', 'ROW_TOTAL', f"{label} row {number_}: a total / subtotal line of the report, "
                        "not data.", file=label, row=number_, values=[short(c, 30) for c in raw if text(c)][:6])
                continue
            values = {f: raw[i] if i < len(raw) else None for f, i in index.items()}
            if context_ref:
                values['mandate_no'] = context_ref
                values.pop('client_name', None)
            composed = self._compose_mandate_no(values)
            if composed:
                values['mandate_no'] = composed
                parts += 1
            extra = {header[i]: raw[i] for i in range(min(len(raw), len(header)))
                     if i not in used and header[i] and text(raw[i])}
            data.append({'row': number_, 'v': values, 'extra': extra})
        table = {'file': label, 'kind': kind, 'rows': data, 'header': header, 'mapping': {f: header[i] for f, i in
                                                                                          index.items()},
                 'unused': unused, 'note': note, 'scores': dict(ranked[:5])}
        if parts:
            self.ev('decision', 'REF_COMPOSED', f"{label}: M2 table export without a mandate number column: the "
                    f"number of {parts} rows is built from '{table['mapping'].get('mandate_prefix', '')}', "
                    f"'{table['mapping']['mandate_seq']}' and '{table['mapping'].get('mandate_kind', '')}' "
                    "(e.g. KP-00031-ENT).", file=label)
        self.tables[kind].append(table)
        self.files.append(table)
        self.ev('info', 'FILE_READ', f"{label}: {len(data)} data rows ({note})" + (f", {blank} blank rows skipped"
                                                                                  if blank else '') + '.', file=label)

    @staticmethod
    def _compose_mandate_no(values):
        """Mandate number of an M2 table export row: prefix-number-type (KP-00031-ENT), when the row has no number."""
        seq = text(values.get('mandate_seq'))
        if text(values.get('mandate_no')) or not seq.isdigit():
            return None
        return '-'.join(filter(None, [text(values.get('mandate_prefix')).upper(), seq.zfill(5),
                                      text(values.get('mandate_kind')).upper()]))

    def _match_headers(self, normalized, fields, filled=None):
        """field -> (column position, alias); each column is used once, exact names before partial ones. An empty
        column (not in `filled`) never takes a field, but its exact name keeps the field from a partial match
        (an empty STATUS must not hand status to STATUS_MUTDATUM)."""
        found, taken, named = {}, set(), set()
        for exact in (True, False):
            for field, aliases in fields.items():
                if field in found or (not exact and field in named):
                    continue
                for alias in aliases:
                    a = norm(alias)
                    for pos, head in enumerate(normalized):
                        if pos in taken or not head:
                            continue
                        if (head == a) if exact else (len(a) > 3 and (head.startswith(a + ' ') or
                                                                     head.endswith(' ' + a))):
                            if filled is not None and pos not in filled:
                                named.add(field)
                                continue
                            found[field] = (pos, alias)
                            taken.add(pos)
                            break
                    if field in found:
                        break
        return found

    @staticmethod
    def _is_total(raw):
        first = norm(next((c for c in raw if text(c)), ''))
        return bool(re.match(r'(total|summe|gesamt|subtotal|zwischensumme|anzahl|count)\b', first))

    # -- 2. clients from mandates and addresses --------------------------------------------------------------

    def build_clients(self):
        self.stage('clients', "Building the clients from the mandates and their addresses")
        addresses = self._address_index()
        mandates = [(t, r) for t in self.tables['mandates'] for r in t['rows']]
        if not mandates:
            if addresses['rows'] and self.config.get('addresses_without_mandate') == 'client':
                self.ev('decision', 'CLIENTS_FROM_ADDRESSES', "No mandate export: clients are built from the "
                        "addresses (Settings > addresses without mandate = client).")
                mandates = [(t, r) for t, r in addresses['rows']]
            else:
                self.ev('error', 'NO_MANDATES', "No mandate export found: no clients can be built, and every "
                        "register row will wait in review.", fix="Export the mandate list from M2 (Mandates > "
                        "evaluation > Excel) into the folder, or set addresses without mandate = client.")
        seen = {}
        used_addresses = set()
        for table, row in mandates:
            self._client_from_mandate(table, row, addresses, seen, used_addresses)
        for table, row in addresses['rows']:
            if id(row) in used_addresses or self.tables['mandates'] == []:
                continue
            key = text(row['v'].get('address_no') or row['v'].get('debtor_no')) or None
            name = self._person_name(row['v'], table['file'], row['row'], key)[0]
            self.ev('info', 'ADDRESS_NO_MANDATE', f"Address {key or '?'} ({name}) has no mandate: not a client "
                    "(M2: not every address has a mandate). It is used only when a register row names it.",
                    key=key, file=table['file'], row=row['row'])

    def _address_index(self):
        index = {'address': {}, 'debtor': {}, 'name': collections.defaultdict(list), 'rows': []}
        for table in self.tables['addresses']:
            for row in table['rows']:
                v = row['v']
                index['rows'].append((table, row))
                for field, bucket in (('address_no', 'address'), ('debtor_no', 'debtor')):
                    key = rkey(v.get(field))
                    if key:
                        if key in index[bucket]:
                            self.ev('warning', 'ADDRESS_DUPLICATE_KEY', f"{field.replace('_', ' ')} {text(v[field])} "
                                    f"appears twice in the addresses ({table['file']} rows "
                                    f"{index[bucket][key][1]['row']} and {row['row']}): the first one is used.",
                                    key=text(v[field]), file=table['file'], row=row['row'])
                        else:
                            index[bucket][key] = (table, row)
                name = norm(v.get('name') or ' '.join(filter(None, [text(v.get('first_name')),
                                                                    text(v.get('last_name'))])))
                if name:
                    index['name'][name].append((table, row))
        return index

    def _person_name(self, v, where, row_no, key):
        """Full name of a person or company row; M2 writes persons as 'Surname, Given name'."""
        name = text(v.get('name'))
        first, last = text(v.get('first_name')), text(v.get('last_name'))
        if first and not last and name and not COMPANY_WORDS.search(name) and ',' not in name:
            full = f"{first} {name}"
            return full, f"Name '{name}' read as the surname of given name '{first}': '{full}'."
        if first or last:
            full = ' '.join(filter(None, [first, last]))
            if name and norm(name) not in (norm(full), norm(f'{last} {first}'), norm(f'{last}, {first}')):
                return name, None  # a company name with a contact person in the name columns
            return full, (f"Name built from given name '{first}' and surname '{last}'." if not name else None)
        match = re.fullmatch(r"([^,]+),\s*([^,]+)", name)
        if match and not COMPANY_WORDS.search(name):
            full = f"{match[2].strip()} {match[1].strip()}"
            return full, f"M2 writes 'Surname, Given name': '{name}' loaded as '{full}'."
        return name, None

    def _client_from_mandate(self, table, row, addresses, seen, used_addresses):
        v, where = row['v'], f"{table['file']} row {row['row']}"
        ref = text(v.get('mandate_no')) or text(v.get('debtor_no')) or text(v.get('address_no'))
        if not text(v.get('mandate_no')) and ref:
            self.ev('decision', 'REF_FALLBACK', f"{where}: no mandate number; the "
                    f"{'debtor' if text(v.get('debtor_no')) else 'address'} number {ref} is used as M2 Reference.",
                    key=ref, file=table['file'], row=row['row'])
        name, note = self._person_name(v, table['file'], row['row'], ref)
        out = {c[0]: None for c in SHEETS['clients']['columns']}
        out['m2_reference'] = ref or None
        out['name'] = name or None
        if note:
            self.change('clients', where, ref, 'Name', text(v.get('name')), name, note)
        if not ref:
            self.to_review('clients', where, None, "No mandate, debtor or address number: the client cannot be "
                           "matched again on a later import.", "Fill M2 Reference with the M2 mandate number.", out)
            return
        if rkey(ref) in self.excluded:
            self.ev('info', 'EXCLUDED', f"{where}: mandate {ref} is excluded (Settings > excluded keys).", key=ref)
            return
        if rkey(ref) in seen:
            first = seen[rkey(ref)]
            self.ev('error', 'DUPLICATE_REF', f"{where}: mandate {ref} is already on {first}. The second row is "
                    "left out.", key=ref, file=table['file'], row=row['row'],
                    fix="Keep one row per mandate in the export (or merge the two in M2).")
            self.to_review('clients', where, ref, f"Mandate {ref} appears twice (first on {first}).",
                           "Merge the two rows into one.", out)
            return
        seen[rkey(ref)] = where
        self.ev('info', 'CLIENT', f"{where}: mandate {ref} '{name}'.", key=ref, file=table['file'], row=row['row'])

        # the address behind the mandate
        address, how = None, None
        # in M2 the debtor of a mandate is an address: its Debtor number is that address's Number
        for field, bucket in (('address_no', 'address'), ('debtor_no', 'debtor'), ('debtor_no', 'address')):
            key = rkey(v.get(field))
            if key and key in addresses[bucket]:
                address, how = addresses[bucket][key], f"{field.replace('_', ' ')} {text(v.get(field))}" + (
                    " = address number" if field == 'debtor_no' and bucket == 'address' else '')
                break
        if not address and table['kind'] == 'addresses':
            address, how = (table, row), 'the row itself'
        if not address and name and len(addresses['name'].get(norm(name), [])) == 1:
            address, how = addresses['name'][norm(name)][0], f"the exact name '{name}'"
        if address:
            used_addresses.add(id(address[1]))
            a = address[1]['v']
            if how != 'the row itself':
                self.ev('decision', 'ADDRESS_LINK', f"Mandate {ref}: address taken from {address[0]['file']} row "
                        f"{address[1]['row']} (matched on {how}).", key=ref, file=address[0]['file'],
                        row=address[1]['row'])
        else:
            a = {}
            if self.tables['addresses']:
                self.ev('warning', 'ADDRESS_MISSING', f"Mandate {ref}: no address found (address no "
                        f"{text(v.get('address_no')) or '-'}, debtor no {text(v.get('debtor_no')) or '-'}): only the "
                        "address fields of the mandate export are used.", key=ref, file=table['file'],
                        row=row['row'], fix="Check the address / debtor number of the mandate in M2.")

        def pick(field):
            mine, theirs = text(v.get(field)), text(a.get(field))
            if mine and theirs and norm(mine) != norm(theirs):
                self.ev('decision', 'VALUE_CONFLICT', f"Mandate {ref}: {field} differs between the mandate "
                        f"('{short(mine, 50)}') and the address ('{short(theirs, 50)}'): the mandate value is kept.",
                        key=ref, field=field, alternatives=[theirs])
            return v.get(field) if mine else a.get(field)

        for field in ('street', 'street2', 'zip', 'city', 'website'):
            out[field] = text(pick(field)) or None
        zip_city = text(pick('zip_city'))
        if zip_city and not (out['zip'] or out['city']):
            # M2 prints country code, postcode and town together: 'MH 96960 Ajeltake Island, Majuro'
            parts = re.fullmatch(r'(?:([A-Z]{2})[\s-]+)?(?:(\S*\d\S*)\s+)?(.+)', zip_city)
            if parts and (parts[1] or parts[2]):
                out['zip'], out['city'] = parts[2] or None, parts[3]
                if parts[1] and not text(pick('country')):
                    v = {**v, 'country': parts[1]}
                self.change('clients', where, ref, 'Zip / City', zip_city, f"{out['zip'] or '-'} | {out['city']}",
                            "Postcode and town split" + (f"; country code {parts[1]} used as the country." if
                                                          parts[1] else '.'))
            else:
                out['city'] = zip_city
                self.change('clients', where, ref, 'City', zip_city, zip_city, "No postcode found: all of it "
                            "loaded as the town.")
        phone = text(pick('phone')) or text(pick('mobile'))
        out['phone'] = phone or None
        if text(pick('phone')) and text(pick('mobile')):
            self.not_loaded_value('clients', where, ref, name, 'Mobile', text(pick('mobile')),
                                  "The template has one phone column: the business phone is loaded.")
        out['email'] = self.convert('clients', where, ref, 'Email', pick('email'), self.conv.email)
        out['country'] = self.convert('clients', where, ref, 'Country', pick('country'), self.conv.country)

        # individual or company
        kind_raw = norm(v.get('mandate_kind') or a.get('address_kind'))
        legal = norm(v.get('legal_form'))
        if kind_raw in INDIVIDUAL_KINDS or (kind_raw.split(' ')[0] if kind_raw else '') in INDIVIDUAL_KINDS:
            out['type'] = 'Individual'
            self.ev('decision', 'TYPE', f"Mandate {ref}: individual (M2 type '{text(v.get('mandate_kind') or a.get('address_kind'))}').",
                    key=ref)
        elif kind_raw in COMPANY_KINDS or legal:
            out['type'] = 'Company'
        elif text(a.get('first_name')) and not text(a.get('name')):
            out['type'] = 'Individual'
            self.ev('decision', 'TYPE', f"Mandate {ref}: individual (the address has a given name and no company "
                    "name).", key=ref)
        else:
            out['type'] = 'Company'
            if kind_raw:
                self.ev('warning', 'TYPE_UNKNOWN', f"Mandate {ref}: M2 type '{text(v.get('mandate_kind'))}' is "
                        "unknown: loaded as a company.", key=ref, fix="Correct Type in the import file if it is a "
                        "person.")

        # entity type from the legal form
        if legal and out['type'] == 'Company':
            entity = next((etype for etype, forms in LEGAL_FORMS.items() if legal in forms or any(
                re.search(rf'\b{re.escape(f)}\b', legal) for f in forms if len(f) > 2)), None)
            if entity:
                out['entity_type'] = entity
                self.ev('decision', 'ENTITY_TYPE', f"Mandate {ref}: legal form '{text(v.get('legal_form'))}' -> "
                        f"entity type {entity}.", key=ref)
                if legal in ('anstalt', 'establishment', 'est'):
                    self.ev('warning', 'ENTITY_TYPE_CHECK', f"Mandate {ref}: an Anstalt (establishment) can be "
                            "company- or foundation-like: loaded as company.", key=ref,
                            fix="Change Entity Type to foundation if its statutes make it one.")
            else:
                self.ev('warning', 'ENTITY_TYPE_UNKNOWN', f"Mandate {ref}: legal form '{text(v.get('legal_form'))}' "
                        "is not known: Entity Type left empty.", key=ref,
                        fix="Fill Entity Type (company, foundation or trust).")

        # operating company: never a default
        holder = text(v.get('holder'))
        company_map = {norm(k): val for k, val in (self.config.get('company_map') or {}).items()
                       if not k.startswith('_') and val}
        review_reason = None
        if holder and norm(holder) in company_map:
            out['company'] = company_map[norm(holder)]
            self.ev('decision', 'COMPANY', f"Mandate {ref}: holder '{holder}' -> operating company "
                    f"'{out['company']}' (Settings > company map).", key=ref)
        elif holder and norm(holder) in {norm(x) for x in company_map.values()}:
            out['company'] = next(x for x in company_map.values() if norm(x) == norm(holder))
            self.ev('decision', 'COMPANY', f"Mandate {ref}: holder '{holder}' is an operating company name.", key=ref)
        elif self.config.get('require_company', True):
            known = ', '.join(k for k in (self.config.get('company_map') or {}) if not k.startswith('_')) or 'none'
            review_reason = (f"Mandate holder '{holder}' is not in the company map (known: {known})." if holder else
                             "No mandate holder: the operating company cannot be decided.")
            self.ev('review', 'COMPANY_UNKNOWN', f"Mandate {ref}: {review_reason} Never put in a default company.",
                    key=ref, value=holder, alternatives=list(company_map.values())[:10],
                    fix=f"Add '{holder or '<holder>'}' to Settings > company map, or fill Operating Company.")
        else:
            self.ev('warning', 'COMPANY_DEFAULT', f"Mandate {ref}: no operating company: the import puts it in the "
                    "company chosen on the import (require company is off).", key=ref)

        # employees
        for field, label in (('advisor', 'Client Advisor'), ('deputy', 'Deputy')):
            out[field] = self.employee(where, ref, label, v.get(field))

        # dates, amounts, other fields
        out['contract_start'] = self.convert('clients', where, ref, 'Contract Start Date', v.get('contract_start'),
                                             self.conv.date)
        out['contract_end'] = self.convert('clients', where, ref, 'Contract End Date', v.get('contract_end'),
                                           self.conv.date)
        out['incorporation_date'] = self.convert('clients', where, ref, 'Incorporation Date',
                                                 pick('incorporation_date'), self.conv.date)
        fy = v.get('fy_end')
        if text(fy):
            fy_date, fy_note = self.conv.date(fy)
            if fy_date:
                out['fy_end'] = fy_date
            elif re.fullmatch(r'\d{1,2}[./\-]\d{1,2}\.?', text(fy)):
                day, month = [int(x) for x in re.findall(r'\d+', text(fy))[:2]]
                try:
                    year = datetime.date.today().year
                    out['fy_end'] = datetime.date(year, month, day)
                    self.change('clients', where, ref, 'Financial Year End', text(fy), out['fy_end'],
                                f"M2 holds day and month only: dated in {year}.")
                except ValueError:
                    self.not_loaded_value('clients', where, ref, name, 'Financial Year End', text(fy), fy_note)
            else:
                self.not_loaded_value('clients', where, ref, name, 'Financial Year End', text(fy), fy_note)
        for field, label in (('share_capital', 'Share Capital'), ('paid_in', 'Paid In')):
            amount, currency, amount_note = self.conv.number(v.get(field))
            if amount is not None:
                out[field] = amount
                if amount_note:
                    self.change('clients', where, ref, label, text(v.get(field)), amount, amount_note)
                cur_field = 'share_capital_currency' if field == 'share_capital' else 'paid_in_currency'
                explicit = self.convert('clients', where, ref, f'{label} Currency', v.get('share_capital_currency'),
                                        self.conv.currency) if v.get('share_capital_currency') else None
                out[cur_field] = explicit or currency
                if not out[cur_field]:
                    self.ev('warning', 'AMOUNT_NO_CURRENCY', f"Mandate {ref}: {label} {amount:,.2f} has no "
                            "currency; Odoo would give it the company currency.", key=ref,
                            fix=f"Fill {label} Currency in the import file.")
            elif text(v.get(field)):
                self.not_loaded_value('clients', where, ref, name, label, text(v.get(field)), amount_note)
        out['registration_number'] = text(v.get('registration_number')) or None
        out['activity'] = text(pick('activity')) or None
        vat = text(v.get('vat'))
        if vat and vat.casefold() not in NOT_APPLICABLE:
            out['vat'] = vat
        elif vat:
            self.change('clients', where, ref, 'Tax ID', vat, '', "Not a tax ID: left empty.")
        lei = text(v.get('lei')).upper().replace(' ', '')
        if lei and re.fullmatch(r'[A-Z0-9]{20}', lei):
            out['lei'] = lei
        elif lei:
            self.not_loaded_value('clients', where, ref, name, 'Legal Entity Identifier', text(v.get('lei')),
                                  "Not a 20-character LEI.")
        tags = [t for t in [text(v.get('tags'))] if t]
        if self.config.get('tag_m2', True):
            tags.append('M2')
        out['tags'] = ', '.join(tags) or None

        # notes: the M2 data with no CSP column
        extra_notes = []
        for field, label in (('holder', 'Mandate holder'), ('jurisdiction', 'Jurisdiction'),
                             ('legal_form', 'Legal form'), ('mandate_kind', 'M2 type'), ('language', 'Language'),
                             ('debtor_no', 'Debtor no'), ('address_no', 'Address no'), ('status', 'Status'),
                             ('bo_name', 'Beneficial owner (M2)')):
            if text(v.get(field)):
                extra_notes.append((label, text(v.get(field))))
        if text(v.get('jurisdiction')):
            self.not_loaded_value('clients', where, ref, name, 'Jurisdiction', text(v.get('jurisdiction')),
                                  "The Clients template has no jurisdiction column: kept in Notes.")
        for header, value in list(row['extra'].items()) + list((address[1]['extra'] if address else {}).items()):
            self.not_loaded_value('clients', where, ref, name, header, text(value), "M2 column with no CSP field.")
            extra_notes.append((header, text(value)))
        notes = [text(v.get('notes')), text(a.get('notes'))]
        if self.config.get('notes_from_unmapped', True) and extra_notes:
            seen_notes = set()
            unique = [(k, val) for k, val in extra_notes if (norm(k), norm(val)) not in seen_notes
                      and not seen_notes.add((norm(k), norm(val)))]
            notes.append('M2 data: ' + '; '.join(f"{k}: {val}" for k, val in unique))
        out['notes'] = '\n'.join(n for n in notes if n) or None

        # closed mandates
        status = norm(v.get('status'))
        active = norm(v.get('active'))
        inactive = active in NO and active != '' or active in CLOSED_STATUS
        end = out['contract_end']
        closed = status in CLOSED_STATUS or inactive or (end and end < datetime.date.today())
        if closed and not self.config.get('include_closed_mandates'):
            why = f"status '{text(v.get('status'))}'" if status in CLOSED_STATUS else \
                f"Active = '{text(v.get('active'))}'" if inactive else f"ended {end:%Y-%m-%d}"
            self.ev('decision', 'MANDATE_CLOSED', f"Mandate {ref}: closed ({why}): not loaded as a client "
                    "(Settings > include closed mandates). It stays in the M2 archive.", key=ref)
            self.closed.append((where, ref, name, why))
            self.register_lookup(ref, v, name, out, closed=True)
            return
        if not out['name']:
            review_reason = "No name."
        if review_reason:
            self.to_review('clients', where, ref, review_reason,
                           "Fill Operating Company (or the company map) and load the row." if 'compan' in
                           review_reason else "Fill the Name.", out)
            self.register_lookup(ref, v, name, out, review=True)
            return
        self.rows['clients'].append(out)
        self.clients[rkey(ref)] = out
        self.register_lookup(ref, v, name, out)

        # a beneficial owner named on the mandate (M2 manual: UBO mandates carry the BO's name)
        bo = text(v.get('bo_name'))
        if bo and self.config.get('ubo_from_mandate', True):
            self.pending_bo.append((where, ref, bo))

    def register_lookup(self, ref, v, name, out, closed=False, review=False):
        state = 'closed' if closed else 'review' if review else 'loaded'
        entry = {'ref': ref, 'name': name, 'state': state}
        self.lookup['mandate'][rkey(ref)] = entry
        for field, bucket in (('debtor_no', 'debtor'), ('address_no', 'address')):
            key = rkey(v.get(field))
            if key:
                self.lookup[bucket].setdefault(key, entry)
        if name:
            self.lookup['name'][norm(name)].append(entry)

    # -- 3. registers ----------------------------------------------------------------------------------------

    def build_registers(self):
        self.stage('registers', "Linking the register rows (officers, owners, banks, contacts) to their clients")
        for where, ref, bo in self.pending_bo:
            bo, note = self._person_name({'name': bo}, '', 0, ref)
            if note:
                self.change('ubos', where, ref, 'Name', '', bo, note)
            out = {c[0]: None for c in SHEETS['ubos']['columns']}
            out.update(client=ref, name=bo, remark='Beneficial owner named on the M2 mandate.')
            existing = [t for t in self.tables['ubos'] for r in t['rows']]
            self.ev('decision', 'UBO_FROM_MANDATE', f"Mandate {ref}: beneficial owner '{bo}' named on the mandate -> "
                    "UBO entry" + (" (merged with the UBO export when the same person is there)." if existing else "."),
                    key=ref)
            self.rows['ubos'].append({**out, '_where': where, '_key': ref, '_from_mandate': True})
        for kind in ['officers'] + REGISTER_KINDS:
            for table in self.tables[kind]:
                for row in table['rows']:
                    sheet = kind
                    if kind == 'officers':
                        sheet = self.route_officer(table, row)
                        if not sheet:
                            continue
                    self.register_row(sheet, table, row)
        for sheet in ('directors', 'ubos', 'shareholders', 'secretaries', 'agents', 'contacts'):
            self.merge_duplicates(sheet)
        self.apply_bank_relations()

    def apply_bank_relations(self):
        with_accounts = set()
        for row in self.rows['bank_accounts']:
            relations = self.bank_relations.get(row['client'], [])
            if not relations:
                continue
            with_accounts.add(row['client'])
            banks = {norm(r['bank_name']): r for r in relations}
            if row.get('bank_name'):
                continue
            if len(banks) == 1:
                bank = next(iter(banks.values()))
                for field, label in (('bank_name', 'Bank'), ('routing_number', 'Routing Number'),
                                     ('swift_code', 'SWIFT / BIC')):
                    if bank[field] and not row.get(field):
                        row[field] = bank[field]
                        self.change('bank_accounts', row['_where'], row['client'], label, '', bank[field],
                                    f"From the client's one M2 bank relationship ({bank['where']}).")
            else:
                self.ev('warning', 'BANK_AMBIGUOUS', f"{row['_where']}: account {row['acc_number']} has no bank and "
                        f"client {row['client']} has {len(banks)} banks in M2 "
                        f"({', '.join(r['bank_name'] for r in banks.values())}): Bank left empty.",
                        key=row['client'], fix="Fill Bank in the import file.")
        for client, relations in self.bank_relations.items():
            if client not in with_accounts:
                for r in relations:
                    self.not_loaded_value('bank_accounts', r['where'], client, r['bank_name'], 'Bank relationship',
                                          r['bank_name'], "No account number for this bank in the exports: an "
                                          "account needs one to load.")

    def route_officer(self, table, row):
        where = f"{table['file']} row {row['row']}"
        function = text(row['v'].get('function'))
        types = {norm(k): val for k, val in (self.config.get('officer_types') or {}).items()
                 if not k.startswith('_') and val}
        if function and norm(function) in types:
            sheet = types[norm(function)]
            if sheet == 'ignore':
                self.ev('decision', 'OFFICER_IGNORED', f"{where}: M2 type '{function}' is ignored (Settings > officer "
                        "types).", file=table['file'], row=row['row'], key=self._row_ref(row['v']))
                return None
            if sheet in SHEETS and sheet not in ('clients', 'bank_accounts', 'bank_access'):
                self.ev('decision', 'OFFICER_ROUTE', f"{where}: M2 type '{function}' -> {SHEETS[sheet]['title']} "
                        "(Settings > officer types).", file=table['file'], row=row['row'], key=self._row_ref(row['v']))
                return sheet
            self.ev('error', 'OFFICER_TYPE_INVALID', f"Settings > officer types: '{function}' -> '{sheet}' is not a "
                    "register sheet.", fix="Use directors, ubos, shareholders, secretaries, agents, contacts, "
                    "signatories or ignore.")
        for pattern, sheet in OFFICER_FUNCTIONS:
            if function and re.search(pattern, function, re.I):
                self.ev('decision', 'OFFICER_ROUTE', f"{where}: function '{function}' -> {SHEETS[sheet]['title']}.",
                        file=table['file'], row=row['row'], key=self._row_ref(row['v']))
                return sheet
        if not function:
            self.ev('decision', 'OFFICER_ROUTE', f"{where}: no function: loaded as a director.", file=table['file'],
                    row=row['row'], key=self._row_ref(row['v']))
            return 'directors'
        self.ev('review', 'OFFICER_FUNCTION_UNKNOWN', f"{where}: function '{function}' is not a director, secretary, "
                "agent, owner or contact.", file=table['file'], row=row['row'], key=self._row_ref(row['v']),
                value=function, fix=f"Add '{function}' to Settings > officer types (e.g. -> directors), or put the "
                "row in the right sheet of the import file by hand.")
        out = {c[0]: None for c in SHEETS['directors']['columns']}
        out.update(client=self._row_ref(row['v']), name=self._person_name(row['v'], '', 0, None)[0],
                   remark=f"M2 function: {function}")
        self.to_review('directors', where, self._row_ref(row['v']), f"Unknown function '{function}'.",
                       "Move it to the right sheet (Directors, Secretaries, Agents...).", out)
        return None

    @staticmethod
    def _row_ref(v):
        return text(v.get('mandate_no')) or text(v.get('debtor_no')) or text(v.get('address_no')) or \
            text(v.get('client_name')) or None

    def resolve_client(self, v, where):
        """(client entry, how, problem) of a register row: by mandate no, debtor no, address no, then exact name."""
        tried = []
        for field, bucket, label in (('mandate_no', 'mandate', 'mandate number'), ('debtor_no', 'debtor',
                                                                                    'debtor number'),
                                     ('address_no', 'address', 'address number')):
            value = text(v.get(field))
            if not value:
                continue
            tried.append(f"{label} {value}")
            entry = self.lookup[bucket].get(rkey(value))
            if entry:
                return entry, f"{label} {value}", None
        # a client name in a number column (M2 lists print the mandate name there on some reports)
        name = text(v.get('client_name')) or next(
            (text(v.get(f)) for f in ('mandate_no', 'debtor_no', 'address_no')
             if text(v.get(f)) and not re.search(r'\d', text(v.get(f)))), '')
        if name:
            tried.append(f"name '{name}'")
            for bucket in ('mandate', 'debtor', 'address'):
                entry = self.lookup[bucket].get(rkey(name))
                if entry:
                    return entry, f"'{name}' read as {bucket} number", None
            entries = self.lookup['name'].get(norm(name), [])
            if len(entries) == 1:
                return entries[0], f"exact name '{name}'", None
            if len(entries) > 1:
                return None, None, (f"{len(entries)} mandates are named '{name}' "
                                    f"({', '.join(e['ref'] for e in entries[:5])}): use the mandate number.",
                                    [e['ref'] for e in entries])
        if not tried:
            return None, None, ("No mandate, debtor or address number and no client name.", [])
        names = list({e['name'] for es in self.lookup['name'].values() for e in es if e['name']})
        guess = difflib.get_close_matches(name, names, n=3, cutoff=0.7) if name else []
        guess += [n for n in names if name and len(name) > 3 and norm(n).startswith(norm(name)) and n not in guess][:3]
        return None, None, (f"No client found by {', '.join(tried)}.", guess)

    def register_row(self, sheet, table, row):
        v, where = row['v'], f"{table['file']} row {row['row']}"
        spec = SHEETS[sheet]
        columns = [c[0] for c in spec['columns']]
        out = {c: None for c in columns}
        ref_hint = self._row_ref(v)
        entry, how, problem = self.resolve_client(v, where)
        key = entry['ref'] if entry else ref_hint

        # person / account name
        if sheet in ('bank_accounts',):
            out['acc_number'] = re.sub(r'\s+', ' ', text(v.get('acc_number'))) or None
            out['name'] = text(v.get('account_name')) or None
            if not out['name'] and out['acc_number']:
                out['name'] = f"{text(v.get('bank_name')) or 'Account'} {text(v.get('currency'))}".strip()
                self.change(sheet, where, key, 'Account Name', '', out['name'], "No account name in M2: built from "
                            "the bank and currency.")
            out['bank_name'] = text(v.get('bank_name')) or None
            out['swift_code'] = text(v.get('swift_code')).replace(' ', '').upper() or None
            if out['swift_code'] and not re.fullmatch(r'[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?', out['swift_code']):
                self.ev('warning', 'SWIFT_FORMAT', f"{where}: '{out['swift_code']}' is not an 8 or 11 character "
                        "BIC.", key=key, file=table['file'], row=row['row'])
            out['routing_number'] = text(v.get('routing_number')) or None
            out['currency'] = self.convert(sheet, where, key, 'Currency', v.get('currency'), self.conv.currency)
            if not out['currency'] and not text(v.get('currency')):
                # M2's Account grid has no currency column: it is usually in the account type or number
                words = ' '.join([text(v.get('account_name')), text(v.get('acc_number'))] +
                                 [text(x) for x in row['extra'].values()])
                codes = {c for c in re.findall(r'(?<![A-Z])([A-Z]{3})(?![A-Z])', words.upper()) if c in CURRENCIES}
                if len(codes) == 1:
                    out['currency'] = codes.pop()
                    self.change(sheet, where, key, 'Currency', '', out['currency'],
                                f"No currency column: read from '{short(words, 60)}'.")
            holder = text(v.get('holder'))
            if holder:
                holder_entry, _how, _p = self.resolve_client({'client_name': holder}, where)
                if holder_entry:
                    out['holder'] = holder_entry['ref']
                else:
                    self.not_loaded_value(sheet, where, key, out['name'], 'Account Holder', holder,
                                          "The holder is not a client: the account is held by the client itself.")
            if out['acc_number'] and re.fullmatch(r'[A-Z]{2}\d{2}[A-Z0-9 ]+', out['acc_number']):
                compact = out['acc_number'].replace(' ', '')
                if not self._iban_ok(compact):
                    self.ev('warning', 'IBAN_CHECK', f"{where}: IBAN {out['acc_number']} fails the check digits.",
                            key=key, file=table['file'], row=row['row'], fix="Check the IBAN in M2.")
        else:
            name, note = self._person_name(v, table['file'], row['row'], key)
            out['name'] = name or None
            if note:
                self.change(sheet, where, key, 'Name', text(v.get('name')), name, note)

        # sheet-specific fields
        type_code = norm(v.get('function')) in {norm(k) for k in (self.config.get('officer_types') or {})}
        if sheet == 'contacts':
            # an M2 type code (CP) routes the row; it is not the person's job title
            out['function'] = (text(v.get('remark')) if type_code else text(v.get('function'))) or None
            out['email'] = self.convert(sheet, where, key, 'Email', v.get('email'), self.conv.email)
            out['phone'] = text(v.get('phone')) or None
        if sheet in START_FIELD:
            out[START_FIELD[sheet]] = self.convert(sheet, where, key, 'Start date', v.get('date_start'),
                                                   self.conv.date)
            out[END_FIELD[sheet]] = self.convert(sheet, where, key, 'End date', v.get('date_end'), self.conv.date)
        if 'date_of_birth' in columns:
            out['date_of_birth'] = self.convert(sheet, where, key, 'Date of Birth', v.get('date_of_birth'),
                                                self.conv.date)
        if 'country' in columns:
            out['country'] = self.convert(sheet, where, key, 'Country', v.get('country'), self.conv.country)
        if 'nationality' in columns:
            out['nationality'] = self.convert(sheet, where, key, 'Nationality', v.get('nationality'),
                                              self.conv.country)
        elif text(v.get('nationality')):
            self.not_loaded_value(sheet, where, key, out['name'], 'Nationality', text(v.get('nationality')),
                                  f"The {spec['title']} sheet has no nationality column.")
        if 'address' in columns:
            out['address'] = text(v.get('address')) or None
        if 'signing_authority' in columns:
            out['signing_authority'] = self.convert(sheet, where, key, 'Signing Authority',
                                                    v.get('signing_authority'), self.conv.signing)
            if not out['signing_authority'] and text(v.get('collective')):
                flag, _n = self.conv.boolean(v.get('collective'))
                if flag:
                    out['signing_authority'] = 'jointly' if flag == 'Yes' else 'singly'
                    self.change(sheet, where, key, 'Signing Authority', f"collective {text(v.get('collective'))}",
                                out['signing_authority'], "M2 'collective' flag: J = joint signature, N = sole.")
                else:
                    self.not_loaded_value(sheet, where, key, None, 'collective', text(v.get('collective')),
                                          "Not a J/N value.")
        if 'position' in columns:
            out['position'] = text(v.get('position') or v.get('function')) or None
        if 'remark' in columns:
            out['remark'] = '; '.join(filter(None, (text(v.get(f)) for f in ('remark', 'remark2', 'remark3')))) \
                or None
            if sheet == 'directors' and text(v.get('function')) and not type_code and not re.fullmatch(
                    r'(director|verwaltungsrat|vr|mitglied|member)', text(v.get('function')), re.I):
                out['remark'] = '; '.join(filter(None, [out['remark'], f"M2 function: {text(v.get('function'))}"]))
        if sheet == 'ubos':
            pct, _cur, pct_note = self.conv.number(v.get('ownership'))
            if pct is not None and 0 < pct <= 1 and '%' not in text(v.get('ownership')):
                self.change(sheet, where, key, 'Ownership (%)', text(v.get('ownership')), pct * 100,
                            "A fraction (0-1): read as a percentage.")
                pct = pct * 100
            if pct is not None and not 0 <= pct <= 100:
                self.not_loaded_value(sheet, where, key, out['name'], 'Ownership (%)', text(v.get('ownership')),
                                      "Not between 0 and 100.")
                pct = None
            out['ownership'] = pct or None
            if pct == 0:
                self.change(sheet, where, key, 'Ownership (%)', '0', '', "0 % read as not known: left empty.")
        if sheet == 'shareholders':
            out['certificate_number'] = text(v.get('certificate_number')) or None
            count, _cur, count_note = self.conv.number(v.get('share_count'))
            raw_count = text(v.get('share_count'))
            if count is not None:
                out['share_count'] = count
                cls = re.sub(r"[\d'.,\s]+", ' ', raw_count).strip()
                if cls and not text(v.get('share_class')) and not re.fullmatch(r'(aktien|shares?|stk|stück)', cls,
                                                                               re.I):
                    out['share_class'] = re.sub(r'\b(aktien|shares?)\b', '', cls, flags=re.I).strip() or None
                    self.change(sheet, where, key, 'Class of Shares', raw_count, out['share_class'],
                                "Class read from the number of shares.")
            elif raw_count:
                self.not_loaded_value(sheet, where, key, out['name'], 'No. of Shares', raw_count, count_note)
            out['share_class'] = out['share_class'] or text(v.get('share_class')) or None
        if sheet == 'signatories':
            out['account'] = text(v.get('account')) or None
            out['signature_type'] = text(v.get('signature_type')) or None
            for field, label in (('sign_right', 'Sign Right'), ('collectively', 'Collectively')):
                out[field] = self.convert(sheet, where, key, label, v.get(field), self.conv.boolean)
            if out['collectively'] is None and out['signature_type']:
                code, _n = self.conv.signing(out['signature_type'])
                if code:
                    out['collectively'] = 'Yes' if code == 'jointly' else 'No'
                    self.change(sheet, where, key, 'Collectively', '', out['collectively'],
                                f"Read from the signature type '{out['signature_type']}'.")
        for header, value in row['extra'].items():
            self.not_loaded_value(sheet, where, key, out.get('name'), header, text(value), "M2 column with no CSP "
                                  "field.")

        # M2 Bank grid: the bank relationship (bank address, clearing no) is a row of its own, the accounts are
        # in the Account grid. A relationship row gives its bank to the client's accounts after all rows are read.
        if sheet == 'bank_accounts' and not out['acc_number'] and out['bank_name'] and entry and \
                entry['state'] == 'loaded':
            self.bank_relations[entry['ref']].append({'bank_name': out['bank_name'],
                                                      'routing_number': out['routing_number'],
                                                      'swift_code': out['swift_code'], 'where': where})
            self.ev('decision', 'BANK_RELATION', f"{where}: bank relationship with {out['bank_name']} (no account "
                    f"number) for client {entry['ref']}: its bank is given to the client's accounts.",
                    key=entry['ref'], file=table['file'], row=row['row'])
            return

        # the decision
        reason, fix = None, None
        if problem:
            reason, alternatives = problem
            fix = "Use the M2 mandate number in Client." + (f" Close names: {', '.join(alternatives)}."
                                                             if alternatives else '')
            self.ev('review', 'CLIENT_NOT_FOUND', f"{where}: {reason}", key=key, file=table['file'], row=row['row'],
                    alternatives=alternatives, fix=fix)
        elif entry['state'] == 'closed':
            self.ev('decision', 'CLIENT_CLOSED', f"{where}: client {entry['ref']} is a closed mandate: the row is "
                    "not loaded.", key=entry['ref'], file=table['file'], row=row['row'])
            self.closed.append((where, entry['ref'], out.get('name'), f"{spec['title']} row of a closed mandate"))
            return
        elif entry['state'] == 'review':
            reason, fix = (f"Client {entry['ref']} is itself in review (Clients sheet).",
                           "Fix the client first, then load this row with it.")
            self.ev('review', 'CLIENT_IN_REVIEW', f"{where}: {reason}", key=entry['ref'], file=table['file'],
                    row=row['row'], fix=fix)
        else:
            self.ev('decision', 'LINK', f"{where} -> client {entry['ref']} ({entry['name']}), found by {how}.",
                    key=entry['ref'], file=table['file'], row=row['row'])
            out['client'] = entry['ref']
        if not reason:
            if sheet == 'bank_accounts' and not out['acc_number']:
                reason, fix = "No account number.", "Fill Account Number (IBAN)."
            elif sheet == 'bank_accounts' and not out['currency']:
                reason, fix = "No currency (required).", "Fill Currency with the ISO code."
            elif not out.get('name'):
                reason, fix = "No name.", "Fill Name."
            elif sheet != 'bank_accounts' and norm(out['name']) == norm(entry['name']) and sheet != 'signatories':
                reason, fix = ("The entry names the client itself instead of the person.",
                               "Put the person's name in Name.")
            elif sheet in START_FIELD and out[START_FIELD[sheet]] and out[END_FIELD[sheet]] and \
                    out[END_FIELD[sheet]] < out[START_FIELD[sheet]]:
                reason, fix = "The end date is before the start date.", "Correct one of the two dates."
            if reason:
                self.ev('review', 'ROW_INVALID', f"{where}: {reason}", key=key, file=table['file'], row=row['row'],
                        fix=fix)
        if reason:
            out['client'] = out['client'] or ref_hint
            self.to_review(sheet, where, key, reason, fix, out, log=False)
            return
        out['_where'], out['_key'] = where, entry['ref']
        self.rows[sheet].append(out)

    @staticmethod
    def _iban_ok(iban):
        if not 15 <= len(iban) <= 34:
            return False
        moved = iban[4:] + iban[:4]
        digits = ''.join(str(int(ch, 36)) for ch in moved)
        return int(digits) % 97 == 1

    def merge_duplicates(self, sheet):
        """The import matches a register entry on client + name: rows of one person in one client are merged."""
        start, end = START_FIELD.get(sheet), END_FIELD.get(sheet)
        groups = collections.OrderedDict()
        for row in self.rows[sheet]:
            groups.setdefault((norm(row['client']), norm(row['name'])), []).append(row)
        merged = []
        for rows in groups.values():
            if len(rows) == 1:
                merged.append(rows[0])
                continue
            rows.sort(key=lambda r: (not r.get('_from_mandate'), r.get(end) is None if end else 0,
                                     (r.get(start) or datetime.date.min) if start else 0), reverse=True)
            keep, others = rows[0], rows[1:]
            periods = []
            for other in others:
                for col, value in other.items():
                    if col.startswith('_') or col in (end, 'remark'):
                        continue
                    if value not in (None, '') and keep.get(col) in (None, ''):
                        keep[col] = value
                if start and (other.get(start), other.get(end)) != (keep.get(start), keep.get(end)) and \
                        not other.get('_from_mandate'):
                    periods.append(f"{text(other.get(start)) or '?'} - {text(other.get(end)) or 'current'}")
            note = ("Also in M2: " + '; '.join(periods)) if periods else ''
            if keep.get('_from_mandate'):
                keep['remark'] = '; '.join(filter(None, [r.get('remark') for r in others if r.get('remark')])) or None
            keep['remark'] = '; '.join(filter(None, [keep.get('remark'), note])) or None
            self.ev('decision', 'MERGED', f"{SHEETS[sheet]['title']}: {len(rows)} rows for {keep['name']} at client "
                    f"{keep['client']} merged into one entry (the import matches on client + name)"
                    + (f"; other periods kept in the Remark: {'; '.join(periods)}." if periods else '.'),
                    key=keep['client'], sources=[r.get('_where') for r in rows])
            self.changes.append({'sheet': SHEETS[sheet]['title'], 'where': ', '.join(r.get('_where') or '' for r in
                                                                                     rows),
                                 'key': keep['client'], 'column': 'Remark', 'from': '', 'to': note,
                                 'note': f"{len(rows)} rows merged."})
            merged.append(keep)
        self.rows[sheet] = merged

    # -- 4. fees and open items (not template data: captured for finance) -------------------------------------

    def build_finance(self):
        self.stage('finance', "Fees and open items: kept for finance and the reconciliation, not loaded")
        today = datetime.date.today()
        for table in self.tables['fees']:
            for row in table['rows']:
                v, where = row['v'], f"{table['file']} row {row['row']}"
                entry, how, _p = self.resolve_client(v, where)
                amount, currency, _n = self.conv.number(v.get('amount'))
                code = text(v.get('fee_code'))
                label = {'abf': 'Annual Basic Fee', 'flf': 'Flat Fee', 'lei': 'LEI renewal'}.get(code.casefold(),
                                                                                              code or 'Fee')
                key = entry['ref'] if entry else self._row_ref(v)
                due, _dn = self.conv.date(v.get('next_due')) if text(v.get('next_due')) else (None, None)
                finish, _fn = self.conv.date(v.get('finish_date')) if text(v.get('finish_date')) else (None, None)
                status = 'ended' if finish and finish < today else 'active'
                self.fee_rows.append({'where': where, 'key': key, 'fee': label, 'amount': amount,
                                      'currency': self.conv.currency(v.get('currency'))[0] or currency,
                                      'period': text(v.get('period')), 'next_due': due, 'text': text(v.get('text')),
                                      'billed_by': text(v.get('billed_by')), 'status': status,
                                      'client_found': bool(entry)})
                if status == 'ended':
                    self.ev('decision', 'FEE_ENDED', f"{where}: {label} for {key or '?'} finished on {finish:%Y-%m-%d}: "
                            "listed as ended, not for the subscriptions.", key=key, file=table['file'], row=row['row'])
                    continue
                self.ev('not_loaded', 'FEE', f"{where}: {label} {text(v.get('amount'))} {text(v.get('currency'))} "
                        f"for {key or '?'}: no column in the client template; listed under Fees for the sale "
                        "subscriptions / recurring invoices.", key=key, file=table['file'], row=row['row'])
                # the LEI fee's due date is the LEI renewal date of the client
                client = self.clients.get(rkey(entry['ref'])) if entry else None
                if code.casefold() == 'lei' and client is not None and due:
                    if not client.get('lei_renewal'):
                        client['lei_renewal'] = due
                        self.change('clients', where, key, 'LEI Renewal Date', text(v.get('next_due')), due,
                                    "Due date of the M2 LEI fee.")
                    elif client['lei_renewal'] != due:
                        self.ev('warning', 'LEI_RENEWAL_CONFLICT', f"{where}: LEI fee due {due:%Y-%m-%d} but client "
                                f"{key} already has renewal {client['lei_renewal']:%Y-%m-%d}: the first is kept.",
                                key=key)
        paid = 0
        for table in self.tables['open_items']:
            has_open = 'amount_open' in table['mapping'] or 'amount_open_dc' in table['mapping']
            for row in table['rows']:
                v, where = row['v'], f"{table['file']} row {row['row']}"
                entry, how, _p = self.resolve_client(v, where)
                raw = v.get('amount_open') if 'amount_open' in table['mapping'] else \
                    v.get('amount_open_dc') if has_open else v.get('amount')
                amount, currency, note = self.conv.number(raw)
                currency = self.conv.currency(v.get('currency'))[0] or currency or '?'
                client = self.clients.get(rkey(entry['ref'])) if entry else None
                company = (client or {}).get('company') or '(not assigned)'
                is_paid = self.conv.boolean(v.get('paid'))[0] == 'Yes' or norm(v.get('status')) in ('paid', 'bezahlt')
                if (amount == 0 or (amount is None and is_paid)) and has_open:
                    paid += 1
                    self.ev('info', 'OPEN_ITEM_PAID', f"{where}: invoice {text(v.get('invoice_no')) or '?'} is paid "
                            "(nothing open): not a receivable.", key=entry['ref'] if entry else None,
                            file=table['file'], row=row['row'])
                    continue
                if amount is None:
                    self.ev('warning', 'OPEN_ITEM_AMOUNT', f"{where}: open amount not readable ({note}).",
                            file=table['file'], row=row['row'])
                    continue
                if is_paid:
                    self.ev('warning', 'OPEN_ITEM_PAID_BUT_OPEN', f"{where}: marked paid in M2 but {amount:,.2f} "
                            f"{currency} is still open (partial payment): counted as open.",
                            key=entry['ref'] if entry else None, file=table['file'], row=row['row'])
                level = text(v.get('reminder_level'))
                self.receivables[company][currency] += amount
                self.ev('not_loaded', 'OPEN_ITEM', f"{where}: open item {text(v.get('invoice_no')) or '?'} "
                        f"{amount:,.2f} {currency} -> {company} receivables (reconciliation total; loaded as opening "
                        "items through accounting, not this file)" + (f"; reminder level {level}" if level and
                                                                      level != '0' else '') + '.',
                        key=entry['ref'] if entry else None, file=table['file'], row=row['row'])
        if paid:
            self.ev('decision', 'OPEN_ITEMS_PAID', f"{paid} paid invoice(s) in the invoice exports left out of the "
                    "receivables.")

    # -- 5. compliance and documents (no template columns: carried for the compliance team) ---------------------

    def build_compliance(self):
        if not self.tables['compliance']:
            return
        self.stage('compliance', "M2 compliance data: kept in the client's Notes and the Compliance sheet")
        for table in self.tables['compliance']:
            for row in table['rows']:
                v, where = row['v'], f"{table['file']} row {row['row']}"
                entry, how, problem = self.resolve_client(v, where)
                key = entry['ref'] if entry else self._row_ref(v)
                ml = self.conv.boolean(v.get('ml_related'))[0] if text(v.get('ml_related')) else None
                lock = self.conv.boolean(v.get('lock'))[0] if text(v.get('lock')) else None
                item = {'where': where, 'key': key, 'client_state': entry['state'] if entry else 'not found',
                        'ml_related': ml or text(v.get('ml_related')), 'risk_category': text(v.get('risk_category')),
                        'risk_country': text(v.get('risk_country')),
                        'lock': lock or text(v.get('lock')), 'lock_text': text(v.get('lock_text')),
                        'note': text(v.get('note'))}
                self.compliance.append(item)
                parts = [f"{label}: {item[f]}" for f, label in (
                    ('ml_related', 'ML-related'), ('risk_category', 'Risk category'),
                    ('risk_country', 'Country of max. risk'), ('lock', 'Lock'), ('lock_text', 'Lock text'),
                    ('note', 'Compliance note')) if item[f]]
                client = self.clients.get(rkey(entry['ref'])) if entry else None
                if client is not None and parts:
                    client['notes'] = '\n'.join(filter(None, [client.get('notes'),
                                                              'M2 compliance: ' + '; '.join(parts)]))
                self.ev('not_loaded', 'COMPLIANCE', f"{where}: M2 compliance of {key or '?'} ({'; '.join(parts) or 'empty'}) "
                        + ("kept in the client's Notes and the Compliance sheet: the risk rating is set in Odoo by "
                           "compliance." if client is not None else
                           f"listed in the Compliance sheet (client {item['client_state']})."),
                        key=key, file=table['file'], row=row['row'])
                if lock == 'Yes':
                    self.ev('warning', 'COMPLIANCE_LOCK', f"{where}: mandate {key} is locked in M2"
                            + (f" ({item['lock_text']})" if item['lock_text'] else '') + ".", key=key,
                            fix="Ask compliance before the client is activated in Odoo.")
                if problem:
                    self.ev('review', 'CLIENT_NOT_FOUND', f"{where}: {problem[0]}", key=key, file=table['file'],
                            row=row['row'], alternatives=problem[1])

    def build_documents(self):
        folders = [p for p in sorted(self.source.iterdir()) if p.is_dir() and not p.name.startswith('.')] \
            if self.source.is_dir() else []
        if not self.tables['documents'] and not folders and not self.tables['inventory']:
            return
        self.stage('documents', "Index of the M2 documents per client (to attach in Odoo)")
        for table in self.tables['documents']:
            for row in table['rows']:
                v, where = row['v'], f"{table['file']} row {row['row']}"
                entry, _how, _problem = self.resolve_client(v, where)
                created, _n = self.conv.date(v.get('created')) if text(v.get('created')) else (None, None)
                maturity, _n = self.conv.date(v.get('maturity')) if text(v.get('maturity')) else (None, None)
                self.documents.append({
                    'where': where, 'key': entry['ref'] if entry else self._row_ref(v),
                    'client_state': entry['state'] if entry else 'not found', 'folder': text(v.get('folder')),
                    'file': text(v.get('file_name')), 'description': text(v.get('description')),
                    'created': created, 'type': text(v.get('doc_type')), 'maturity': maturity, 'on_disk': ''})
                self.ev('info', 'DOCUMENT', f"{where}: document '{text(v.get('file_name'))}' of "
                        f"{entry['ref'] if entry else self._row_ref(v) or '?'}.",
                        key=entry['ref'] if entry else self._row_ref(v), file=table['file'], row=row['row'])
        listed = {(rkey(d['key']), norm(d['file'])) for d in self.documents}
        for folder in folders:
            entry = None
            for bucket in ('mandate', 'debtor', 'address'):
                entry = entry or self.lookup[bucket].get(rkey(folder.name))
            if not entry:
                self.ev('warning', 'FOLDER_NOT_CLIENT', f"Folder '{folder.name}': not a mandate, debtor or address "
                        "number of the exports: its files are not indexed.", file=folder.name,
                        fix="Name the folder after the M2 mandate number (as M2's document store does).")
                continue
            files = [f for f in sorted(folder.rglob('*')) if f.is_file() and not f.name.startswith(('.', '~$'))]
            for f in files:
                rel = f.relative_to(self.source)
                if (rkey(entry['ref']), norm(f.name)) in listed:
                    for d in self.documents:
                        if rkey(d['key']) == rkey(entry['ref']) and norm(d['file']) == norm(f.name):
                            d['on_disk'] = str(rel)
                            d['folder'] = d['folder'] or (str(rel.parent.relative_to(folder.name))
                                                          if rel.parent != Path(folder.name) else '')
                    continue
                self.documents.append({
                    'where': f"folder {folder.name}", 'key': entry['ref'], 'client_state': entry['state'],
                    'folder': str(rel.parent.relative_to(folder.name)) if rel.parent != Path(folder.name) else '',
                    'file': f.name, 'description': '', 'type': f.suffix.lstrip('.').upper(),
                    'created': datetime.date.fromtimestamp(f.stat().st_mtime), 'maturity': None,
                    'on_disk': str(rel)})
            self.ev('decision', 'DOCUMENTS_FOLDER', f"Folder '{folder.name}' -> client {entry['ref']} "
                    f"({entry['name']}): {len(files)} file(s) indexed for attaching in Odoo.", key=entry['ref'],
                    file=folder.name)
        self.build_inventory(listed)
        missing = [d for d in self.documents if not d['on_disk'] and not d['where'].startswith(('folder',
                                                                                                'archive'))]
        if missing and (folders or self.tables['inventory']):
            self.ev('warning', 'DOCUMENTS_MISSING', f"{len(missing)} document(s) of the document export are not in "
                    "the copied folders.", fix="Copy the M2 document store folders next to the exports.")

    def build_inventory(self, listed):
        """Files of the archive inventory, per mandate folder: indexed without copying the archive."""
        if not self.tables['inventory']:
            return
        per_folder = collections.OrderedDict()
        tops = set()                                  # top folders of the drive = mandate holders
        for table in self.tables['inventory']:
            for row in table['rows']:
                raw = text(row['v'].get('path'))
                parts = [x for x in re.split(r'[\\/]+', raw) if x and not re.fullmatch(r'[A-Za-z]:', x)]
                if len(parts) < 2 or parts[-1].lower().endswith('.lnk'):   # loose root files, shortcuts
                    continue
                tops.add(parts[0])
                pos = next((i for i, x in enumerate(parts[:-1]) if MANDATE_RE.fullmatch(x)
                            or any(self.lookup[b].get(rkey(x)) for b in ('mandate', 'debtor'))), None)
                if pos is None:
                    per_folder.setdefault(('', parts[0] if len(parts) > 1 else ''), []).append((table, row, parts,
                                                                                                None))
                    continue
                holder = parts[0] if pos else ''        # the top folder of the drive: the mandate holder
                per_folder.setdefault((holder, parts[pos]), []).append((table, row, parts, pos))
        clients = bool(self.lookup['mandate'])
        seen, copies, copied = set(), collections.Counter(), set()   # copies: same mandate, path, name and size
        for (holder, folder), files in per_folder.items():
            if files[0][3] is None:
                self.ev('warning', 'ARCHIVE_NOT_MANDATE', f"Archive folder '{folder or '(root)'}': {len(files)} "
                        "file(s) not under a folder named like a mandate number: not indexed.", file=files[0][0]['file'],
                        fix="Check the folder; mandate folders are named like KP-00031-ENT.")
                continue
            entry = None
            for bucket in ('mandate', 'debtor', 'address'):
                entry = entry or self.lookup[bucket].get(rkey(folder))
            key = entry['ref'] if entry else folder
            seen.add(rkey(key))
            for table, row, parts, pos in files:
                name, sub = parts[-1], '/'.join(parts[pos + 1:-1])
                same = (rkey(key), norm(sub), norm(name), text(row['v'].get('size')))
                if same in copied:
                    copies[holder] += 1
                    continue
                copied.add(same)
                modified, _n = self.conv.date(row['v'].get('modified')) if text(row['v'].get('modified')) \
                    else (None, None)
                on_disk = 'archive: ' + '\\'.join(parts)
                if (rkey(key), norm(name)) in listed:
                    for d in self.documents:
                        if rkey(d['key']) == rkey(key) and norm(d['file']) == norm(name) and not d['on_disk']:
                            d['on_disk'], d['folder'] = on_disk, d['folder'] or sub
                    continue
                self.documents.append({
                    'where': f"archive {holder}".strip(), 'key': key,
                    'client_state': entry['state'] if entry else ('not in the exports' if clients else 'archive only'),
                    'folder': sub, 'file': name, 'description': '', 'type': Path(name).suffix.lstrip('.').upper(),
                    'created': modified, 'maturity': None, 'on_disk': on_disk})
            if entry:
                self.ev('decision', 'ARCHIVE_FOLDER', f"Archive folder '{holder + chr(92) if holder else ''}{folder}' "
                        f"-> client {entry['ref']} ({entry['name']}): {len(files)} file(s) indexed.", key=entry['ref'],
                        file=files[0][0]['file'])
            else:
                self.ev('warning' if clients else 'info', 'ARCHIVE_FOLDER_UNKNOWN', f"Archive folder "
                        f"'{folder}'{' of ' + holder if holder else ''}: {len(files)} file(s); "
                        + ("not a mandate of the exports (closed, or exported under another number)." if clients
                           else "indexed under its folder name (archive only, no mandate export in this run)."),
                        key=folder, file=files[0][0]['file'],
                        fix="Add the mandate to the exports, or check the folder name." if clients else None)
        self.check_holders(tops)
        for holder, count in copies.items():
            self.ev('info', 'ARCHIVE_COPY', f"Archive '{holder}': {count} file(s) identical to a file of the same "
                    "mandate already indexed (same sub-folder, name and size): indexed once.")
        if clients:
            without = sorted({e['ref'] for e in self.lookup['mandate'].values()
                              if rkey(e['ref']) not in seen and e.get('state') != 'closed'})
            if without:
                self.ev('warning', 'ARCHIVE_NO_FOLDER', f"{len(without)} mandate(s) of the exports have no folder in "
                        f"the archive inventory: {', '.join(without[:20])}{' …' if len(without) > 20 else ''}.",
                        fix="Check the archive path, or the mandate holder folder the inventory was made of.")

    def check_holders(self, tops):
        """Compare the inventory's holders with archive_holders.txt (one per line) in the source folder, when it
        is there: an inventory still being written, or made of part of the drive, then shows at once."""
        listed = self.source / 'archive_holders.txt'
        if not listed.is_file():
            return
        squash = lambda x: re.sub(r'[\s_-]+', '', x).upper()      # NOVA-PH2 = NOVA-PH 2
        want = [x.strip() for x in listed.read_text(encoding='utf-8-sig').splitlines()
                if x.strip() and not x.lstrip().startswith('#')]
        have = {squash(t) for t in tops}
        missing = [w for w in want if squash(w) not in have]
        extra = sorted(t for t in tops if squash(t) not in {squash(w) for w in want})
        if missing:
            self.ev('warning', 'ARCHIVE_HOLDER_MISSING', f"{len(missing)} of {len(want)} mandate holders of "
                    f"archive_holders.txt are not in the archive inventory: {', '.join(missing)}.",
                    fix="Wait until the inventory has finished, or list the whole drive (-Root 'Z:\\').")
        else:
            self.ev('info', 'ARCHIVE_HOLDERS', f"All {len(want)} mandate holders of archive_holders.txt are in the "
                    "archive inventory.")
        if extra:
            self.ev('info', 'ARCHIVE_HOLDER_EXTRA', f"Archive folders not in archive_holders.txt: {', '.join(extra)}.")

    # -- conversion with journaling --------------------------------------------------------------------------

    def convert(self, sheet, where, key, column, raw, func):
        if raw in (None, '') or (isinstance(raw, str) and not raw.strip()):
            return None
        value, note = func(raw)
        if value is None and note:
            self.not_loaded_value(sheet, where, key, None, column, text(raw), note)
        elif note:
            self.change(sheet, where, key, column, text(raw), value, note)
        return value

    def employee(self, where, ref, label, raw):
        value = text(raw)
        if not value:
            return None
        emap = {norm(k): v for k, v in (self.config.get('employee_map') or {}).items() if not k.startswith('_')}
        if not emap:
            return value
        if norm(value) in emap:
            name = emap[norm(value)]
            if norm(name) != norm(value):
                self.change('clients', where, ref, label, value, name, "M2 abbreviation -> employee (Settings > "
                            "employee map).")
            return name
        if norm(value) in {norm(x) for x in emap.values()}:
            return value
        self.ev('warning', 'EMPLOYEE_UNKNOWN', f"Mandate {ref}: {label} '{value}' is not in the employee map: left "
                "empty.", key=ref, value=value, fix=f"Add '{value}' to Settings > employee map.")
        self.not_loaded_value('clients', where, ref, None, label, value, "Not in the employee map.")
        return None

    def change(self, sheet, where, key, column, old, new, note):
        title = SHEETS[sheet]['title'] if sheet in SHEETS else sheet
        self.changes.append({'sheet': title, 'where': where, 'key': key, 'column': column, 'from': text(old),
                             'to': text(new), 'note': note})
        self.ev('change', 'VALUE_CHANGED', f"{where}: {column} '{short(old, 60)}' -> '{short(new, 60)}'. {note}",
                key=key, column=column, old=text(old), new=text(new))

    def not_loaded_value(self, sheet, where, key, name, column, value, note):
        title = SHEETS[sheet]['title'] if sheet in SHEETS else sheet
        self.not_loaded.append({'sheet': title, 'where': where, 'key': key, 'name': name, 'column': column,
                                'value': value, 'note': note})
        self.ev('not_loaded', 'VALUE_NOT_LOADED', f"{where}: {column} '{short(value, 60)}' not loaded. {note or ''}",
                key=key, column=column, value=value)

    def to_review(self, sheet, where, key, reason, fix, out, log=True):
        self.review[sheet].append({'where': where, 'key': key, 'reason': reason, 'fix': fix, 'row': out})
        if log:
            self.ev('review', 'TO_REVIEW', f"{where}: to review ({SHEETS[sheet]['title']}): {reason}", key=key,
                    fix=fix)

    # -- 5. checks of the result -----------------------------------------------------------------------------

    def final_checks(self):
        self.stage('checks', "Checking the result against the template rules")
        names = collections.defaultdict(list)
        for row in self.rows['clients']:
            names[norm(row['name'])].append(row['m2_reference'])
        for name, refs in names.items():
            if len(refs) > 1:
                self.ev('warning', 'SAME_NAME', f"{len(refs)} clients share the name '{name}' ({', '.join(refs)}): "
                        "they load as separate clients because their M2 References differ.", key=refs[0],
                        alternatives=refs)
        for sheet in ORDER:
            spec = SHEETS[sheet]
            required = [c for c in spec['columns'] if c[4]]
            kept = []
            for row in self.rows[sheet]:
                missing = [c[1] for c in required if row.get(c[0]) in (None, '')]
                if missing:
                    where = row.get('_where') or f"client {row.get('m2_reference') or row.get('client')}"
                    self.to_review(sheet, where, row.get('_key') or row.get('m2_reference'),
                                   f"Required column(s) empty: {', '.join(missing)}.", "Fill them.", row)
                else:
                    kept.append(row)
            self.rows[sheet] = kept
            if len(self.rows[sheet]) > 5000:
                self.ev('warning', 'TOO_MANY_ROWS', f"{spec['title']}: {len(self.rows[sheet])} rows; the template "
                        "has drop-downs for 5,000. The import reads them, but split the file if Odoo is slow.")
        clients_with_rows = collections.Counter(r.get('client') for s in REGISTER_KINDS for r in self.rows.get(s, []))
        for row in self.rows['clients']:
            if row['type'] == 'Company' and not clients_with_rows.get(row['m2_reference']):
                self.ev('info', 'CLIENT_NO_REGISTERS', f"Client {row['m2_reference']} ({row['name']}) has no "
                        "officers, owners or bank accounts in the exports.", key=row['m2_reference'])

    # -- run -------------------------------------------------------------------------------------------------

    def run(self):
        self.ev('info', 'START', f"M2 migration of {self.source} -> {self.out}", spec=SPEC_PATH,
                config={k: v for k, v in self.config.items() if not k.startswith('_')})
        try:
            self.discover()
            self.build_clients()
            self.build_registers()
            self.build_finance()
            self.build_compliance()
            self.build_documents()
            self.final_checks()
            self.stage('write', "Writing the import and review files")
            self.out.mkdir(parents=True, exist_ok=True)
            self.write()
            summary = self.summary()
            (self.out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str),
                                                   encoding='utf-8')
            self.ev('info', 'DONE', f"Done: {summary['totals']['import']} rows to import, "
                    f"{summary['totals']['review']} to review, {self.journal.counts['error']} errors.")
            return summary
        except Exception as exc:  # noqa: BLE001 - the UI must show the crash with its traceback
            self.ev('error', 'CRASH', f"The run stopped: {exc}", trace=traceback.format_exc())
            raise
        finally:
            self.journal.close()

    def summary(self):
        sheets = {k: {'title': SHEETS[k]['title'], 'import': len(self.rows[k]), 'review': len(self.review[k])}
                  for k in ORDER}
        return {
            'source': str(self.source), 'out': str(self.out), 'spec': SPEC_PATH,
            'finished': datetime.datetime.now().isoformat(timespec='seconds'),
            'sheets': sheets,
            'totals': {'import': sum(s['import'] for s in sheets.values()),
                       'review': sum(s['review'] for s in sheets.values()),
                       'changes': len(self.changes), 'not_loaded': len(self.not_loaded), 'closed': len(self.closed),
                       'fees': len(self.fee_rows), 'compliance': len(self.compliance),
                       'documents': len(self.documents)},
            'levels': dict(self.journal.counts),
            'codes': dict(collections.Counter(e['code'] for e in self.journal.events)),
            'files': [{'file': t['file'], 'kind': t['kind'], 'rows': len(t['rows']), 'note': t['note'],
                       'mapping': t['mapping'], 'unused': t['unused'], 'scores': t['scores']} for t in self.files],
            'review': {k: [{**{x: y for x, y in r.items() if x != 'row'},
                            'row': {a: jsonable(b) for a, b in r['row'].items() if not a.startswith('_')}}
                           for r in self.review[k]] for k in ORDER},
            'clients': [{a: jsonable(b) for a, b in r.items() if not a.startswith('_')} for r in self.rows['clients']],
            'records': dict(self.records),
            'receivables': {c: dict(v) for c, v in self.receivables.items()},
            'fees': [{k: jsonable(v) for k, v in f.items()} for f in self.fee_rows],
            'compliance': self.compliance,
            'documents': [{k: jsonable(v) for k, v in d.items()} for d in self.documents],
            'closed': [{'where': w, 'key': k, 'name': n, 'why': y} for w, k, n, y in self.closed],
            'outputs': [IMPORT_FILE, REVIEW_FILE, 'journal.jsonl', 'summary.json'],
        }

    # -- writing ---------------------------------------------------------------------------------------------

    def write(self):
        bold, fill = Font(bold=True), PatternFill('solid', fgColor='DDE7F0')
        book = openpyxl.Workbook()
        book.remove(book.active)
        for sheet in ORDER:
            spec = SHEETS[sheet]
            if not self.rows[sheet] and sheet in ('bank_access',):
                continue
            ws = book.create_sheet(spec['title'])
            self._table(ws, [c[1] for c in spec['columns']],
                        [[r.get(c[0]) for c in spec['columns']] for r in self.rows[sheet]], bold, fill)
        book.save(self.out / IMPORT_FILE)
        self.ev('info', 'WRITTEN', f"{IMPORT_FILE}: " + ', '.join(f"{SHEETS[k]['title']} {len(self.rows[k])}"
                                                                    for k in ORDER if self.rows[k]), file=IMPORT_FILE)

        review = openpyxl.Workbook()
        readme = review.active
        readme.title = 'Read Me'
        lines = [
            "M2 Mandat -> CSP import: rows left out, values adjusted and every decision",
            "",
            f"Source: {self.source}",
            f"Template: {SPEC_PATH}",
            f"{IMPORT_FILE} holds every row that can load as it is. This file holds the rest.",
            "Review sheets: fix the row (the Fix column says how), copy the template columns (from the third "
            "column on) into the same sheet of the import file and load it again.",
            "Changes: values converted or cleaned on the way. Not Loaded: M2 values with no CSP column or not "
            "readable. Decisions: the full journal of the run. Files: how each export was read.",
            "Fees and Receivables: M2 finance data for the subscriptions and the reconciliation per company.",
            "Compliance: M2 ML / risk data (no template column; also in the client's Notes). Documents: the M2 "
            "document index per client, for attaching the files in Odoo.",
            "",
            "Rows per sheet (import file / review):",
        ] + [f"  {SHEETS[k]['title']}: {len(self.rows[k])} / {len(self.review[k])}" for k in ORDER] + [
            f"  Closed mandates not loaded: {len(self.closed)}",
            "", "Journal: " + ', '.join(f"{k} {v}" for k, v in self.journal.counts.items()),
        ]
        for line in lines:
            readme.append([line])
        readme['A1'].font = Font(bold=True, size=13)
        readme.column_dimensions['A'].width = 140
        for sheet in ORDER:
            if not self.review[sheet]:
                continue
            spec = SHEETS[sheet]
            ws = review.create_sheet(f"Review {spec['title']}"[:31])
            self._table(ws, ['Source', 'M2 key', 'Reason', 'Fix'] + [c[1] for c in spec['columns']],
                        [[r['where'], r['key'], r['reason'], r['fix']] + [r['row'].get(c[0]) for c in spec['columns']]
                         for r in self.review[sheet]], bold, fill)
        self._table(review.create_sheet('Changes'), ['Sheet', 'Source', 'M2 key', 'Column', 'M2 value',
                                                     'Import value', 'Note'],
                    [[c['sheet'], c['where'], c['key'], c['column'], c['from'], c['to'], c['note']]
                     for c in self.changes], bold, fill)
        self._table(review.create_sheet('Not Loaded'), ['Sheet', 'Source', 'M2 key', 'Name', 'M2 column', 'Value',
                                                        'Note'],
                    [[n['sheet'], n['where'], n['key'], n['name'], n['column'], n['value'], n['note']]
                     for n in self.not_loaded], bold, fill)
        self._table(review.create_sheet('Closed Mandates'), ['Source', 'M2 key', 'Name', 'Why'],
                    [list(c) for c in self.closed], bold, fill)
        self._table(review.create_sheet('Fees'), ['Source', 'M2 key', 'Fee', 'Amount', 'Currency', 'Period',
                                                  'Next due', 'Text', 'Billed by', 'Status', 'Client found'],
                    [[f['where'], f['key'], f['fee'], f['amount'], f['currency'], f['period'], f['next_due'],
                      f['text'], f['billed_by'], f['status'], 'Yes' if f['client_found'] else 'No']
                     for f in self.fee_rows], bold, fill)
        if self.compliance:
            self._table(review.create_sheet('Compliance'), ['Source', 'M2 key', 'Client', 'ML-related',
                                                            'Risk category', 'Country of max. risk', 'Lock',
                                                            'Lock text', 'Note'],
                        [[c['where'], c['key'], c['client_state'], c['ml_related'], c['risk_category'],
                          c['risk_country'], c['lock'], c['lock_text'], c['note']] for c in self.compliance],
                        bold, fill)
        if self.documents:
            self._table(review.create_sheet('Documents'), ['Source', 'M2 key', 'Client', 'Folder', 'File',
                                                           'Description', 'Type', 'Created', 'Maturity',
                                                           'File on disk'],
                        [[d['where'], d['key'], d['client_state'], d['folder'], d['file'], d['description'],
                          d['type'], d['created'], d['maturity'], d['on_disk']] for d in self.documents], bold, fill)
        self._table(review.create_sheet('Receivables'), ['Operating company', 'Currency', 'M2 open amount'],
                    [[c, cur, amount] for c, v in self.receivables.items() for cur, amount in v.items()], bold, fill)
        self._table(review.create_sheet('Files'), ['File', 'Read as', 'Rows', 'Format', 'Columns used',
                                                   'Columns not used', 'Kind scores'],
                    [[t['file'], t['kind'], len(t['rows']), t['note'],
                      ', '.join(f"{h} -> {f}" for f, h in t['mapping'].items()),
                      ', '.join(u['column'] for u in t['unused']),
                      ', '.join(f"{k} {s}" for k, s in t['scores'].items())] for t in self.files], bold, fill)
        self._table(review.create_sheet('Decisions'), ['#', 'Level', 'Stage', 'Rule', 'M2 key', 'File', 'Row',
                                                       'Message', 'Fix'],
                    [[e['seq'], e['level'], e['stage'], e['code'], e.get('key'), e.get('file'), e.get('row'),
                      e['message'], e.get('fix')] for e in self.journal.events if e['level'] != 'info' or
                     e['code'] in ('STAGE', 'START', 'FILE_READ')], bold, fill)
        review.save(self.out / REVIEW_FILE)
        self.ev('info', 'WRITTEN', f"{REVIEW_FILE}: {sum(len(v) for v in self.review.values())} rows to review, "
                f"{len(self.changes)} changes, {len(self.not_loaded)} values not loaded.", file=REVIEW_FILE)

    @staticmethod
    def _table(ws, header, rows, bold, fill):
        ws.append(header)
        for cell in ws[1]:
            cell.font, cell.fill = bold, fill
            cell.alignment = Alignment(vertical='top')
        for row in rows:
            ws.append([v.date() if isinstance(v, datetime.datetime) else
                       (', '.join(map(str, v)) if isinstance(v, (list, tuple)) else v) for v in row])
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                if isinstance(cell.value, (datetime.date, datetime.datetime)):
                    cell.number_format = 'yyyy-mm-dd'
        for i, title in enumerate(header, start=1):
            width = max([len(str(title))] + [len(str(r[i - 1])) for r in rows[:200] if r[i - 1] is not None])
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = min(max(width + 2, 10), 60)
        ws.freeze_panes = 'A2'


def load_config(path):
    path = Path(path) if path else DEFAULT_CONFIG
    if path.is_file():
        return json.loads(path.read_text(encoding='utf-8'))
    return {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('source', help='folder with the M2 exports')
    parser.add_argument('out', nargs='?', help='output folder (default: the source folder)')
    parser.add_argument('--config', default=str(DEFAULT_CONFIG))
    parser.add_argument('--quiet', action='store_true', help='print only the summary')
    args = parser.parse_args(argv)
    out = Path(args.out or args.source)
    out.mkdir(parents=True, exist_ok=True)
    marks = {'error': 'ERR', 'review': 'REV', 'warning': 'WRN', 'decision': 'DEC', 'change': 'CHG',
             'not_loaded': 'N/L', 'info': '   '}

    def show(event):
        if not args.quiet or event['level'] == 'error':
            print(f"{event['t']:7.2f} {marks[event['level']]} {event['message']}")

    job = Migration(args.source, out, load_config(args.config), listener=show, journal_path=out / 'journal.jsonl')
    summary = job.run()
    print()
    for sheet in ORDER:
        s = summary['sheets'][sheet]
        if s['import'] or s['review']:
            print(f"{s['title']:16} {s['import']:5} to import  {s['review']:5} to review")
    print('Journal: ' + ', '.join(f"{k} {v}" for k, v in summary['levels'].items()))
    print(f"Files in {out}: {IMPORT_FILE}, {REVIEW_FILE}")
    return 1 if summary['levels'].get('error') else 0


if __name__ == '__main__':
    sys.exit(main())
