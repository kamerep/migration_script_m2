#!/usr/bin/env python3
"""Writes a small set of fictitious M2 exports to try the migration and its UI.

    python3 make_sample_m2.py [<folder>]        default: ./sample_m2

German headers, a report title above the header, 'Surname, Given name' persons, Swiss amounts, a CSV in
Windows-1252 with semicolons, and on purpose the cases the migration must catch: a mandate twice, an unknown
mandate holder, a closed mandate, a resignation before the appointment, a register row for an unknown client,
an invalid IBAN, an unknown country and an export that is not M2 data. All names are invented.
"""
import datetime
import sys
from pathlib import Path

import openpyxl


def sheet(path, header, rows, title=None):
    book = openpyxl.Workbook()
    ws = book.active
    ws.title = 'Tabelle1'
    if title:
        ws.append([title])
        ws.append([f"Gedruckt am {datetime.date.today():%d.%m.%Y}"])
        ws.append([])
    ws.append(header)
    for row in rows:
        ws.append(row)
    book.save(path)


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent / 'sample_m2')
    out.mkdir(parents=True, exist_ok=True)
    d = datetime.date

    sheet(out / 'Adressen.xlsx',
          ['Adress Nr', 'Debitor Nr', 'Firma', 'Nachname', 'Vorname', 'Adresstyp', 'Strasse', 'PLZ', 'Ort', 'Land',
           'Telefon', 'Natel', 'E-Mail', 'Sprache', 'Anrede'],
          [[1001, 50001, 'Alpenblick Holding AG', '', '', 'GES', 'Bahnhofstrasse 12', '8001', 'Zürich', 'Schweiz',
            '+41 44 123 45 67', '', 'info@alpenblick.example', 'D', ''],
           [1002, 50002, '', 'Muster', 'Hans', 'KLI', 'Seestrasse 4', '6300', 'Zug', 'CH', '', '+41 79 555 11 22',
            'Hans Muster <hans.muster@example.ch>', 'D', 'Herr'],
           [1003, 50003, 'Harbour Bridge Trading Limited', '', '', 'GES', '88 Queensway', '', 'Hong Kong',
            'Hongkong', '+852 2123 4567', '', 'office@harbour.example', 'E', ''],
           [1004, 50004, 'Desert Rose FZCO', '', '', 'GES', 'Dubai Silicon Oasis', '', 'Dubai', 'VAE', '', '',
            'contact@desertrose.example; accounts@desertrose.example', 'E', ''],
           [1005, 50005, 'Blue Lagoon Foundation', '', '', 'GES', 'Aeulestrasse 5', '9490', 'Vaduz', 'Liechtenstein',
            '', '', '', 'D', ''],
           [1006, 50006, 'Old Mill Trading GmbH', '', '', 'GES', 'Mühleweg 1', '3000', 'Bern', 'Schweiz', '', '', '',
            'D', ''],
           [1007, 50007, 'Nordlicht Consulting GmbH', '', '', 'GES', 'Hafenstrasse 9', '20457', 'Hamburg',
            'Atlantis', '', '', 'nordlicht@example.de', 'D', ''],
           [1008, None, 'Keller, Anna', '', '', 'KLI', 'Rue du Lac 3', '1200', 'Genève', 'Suisse', '', '', '', 'F',
            'Frau']],
          title='Adressliste')

    sheet(out / 'Mandate.xlsx',
          ['Mandat Nr', 'Debitor Nr', 'Mandatsname', 'Mandatsträger', 'Art', 'Rechtsform', 'Domizil',
           'Datum Mandatsvertrag', 'Mandatsende', 'Verantwortlicher', 'Stellvertreter', 'Wirtschaftlich Berechtigter',
           'Aktienkapital', 'Gründungsdatum', 'Geschäftsjahresende', 'Status', 'Ablageort'],
          [['M-0001', 50001, 'Alpenblick Holding AG', 'IT', 'GES', 'AG', 'Zug', d(2015, 3, 1), None, 'PK', 'AB',
            'Hans Muster', "CHF 100'000.00", '12.05.2011', '31.12', 'Aktiv', 'Schrank 3'],
           ['M-0002', 50002, 'Muster, Hans', 'IT', 'KLI', '', 'CH', '01.07.2018', None, 'PK', '', '', '', '', '',
            'Aktiv', ''],
           ['M-0003', 50003, 'Harbour Bridge Trading Limited', 'HK', 'GES', 'LTD', 'Hong Kong', '06/21/2019', None,
            'MW', 'PK', '', '10,000.00 HKD', '2019-06-21', '31.03', 'Aktiv', ''],
           ['M-0004', 50004, 'Desert Rose FZCO', 'XY', 'GES', 'FZCO', 'Dubai', '15.01.2021', None, 'ZZ', '', '',
            '50000', '', '', 'Aktiv', ''],
           ['M-0005', 50005, 'Blue Lagoon Foundation', 'IT', 'GES', 'STI', 'Liechtenstein', '03.09.2012', None,
            'AB', '', 'Keller, Anna', '', '', '31.12', 'Aktiv', ''],
           ['M-0006', 50006, 'Old Mill Trading GmbH', 'IT', 'GES', 'GMBH', 'Bern', '01.01.2010', '31.12.2020', 'PK',
            '', '', '20000 CHF', '', '', 'Saldiert', ''],
           ['M-0003', 50003, 'Harbour Bridge Trading Ltd (copy)', 'HK', 'GES', 'LTD', '', '', None, '', '', '', '',
            '', '', 'Aktiv', ''],
           ['M-0007', 50007, 'Nordlicht Consulting GmbH', 'DE', 'GES', 'Holding', 'Hamburg', '2022-02-30', None,
            'PK', '', '', '', '', '', 'Aktiv', '']],
          title='Mandatsliste per Stichtag')

    sheet(out / 'Organe.xlsx',
          ['Mandat Nr', 'Name', 'Vorname', 'Funktion', 'Eintritt', 'Austritt', 'Zeichnungsart', 'Nationalität',
           'Geburtsdatum'],
          [['M-0001', 'Muster', 'Hans', 'Präsident des Verwaltungsrates', '12.05.2011', '', 'Einzelunterschrift',
            'CH', '14.03.1961'],
           ['M-0001', 'Keller', 'Anna', 'Mitglied', '01.01.2016', '', 'Kollektivunterschrift zu zweien', 'Schweiz',
            ''],
           ['M-0001', 'Keller', 'Anna', 'Mitglied', '01.01.2012', '31.12.2014', 'Kollektiv zu zweien', 'CH', ''],
           ['M-0003', 'Zhang', 'Wei', 'Director', '21.06.2019', '', 'Einzel', 'China', '14.03.1981'],
           ['M-0003', 'Harbour Secretaries Ltd', '', 'Secretary', '21.06.2019', '', '', '', ''],
           ['M-0005', 'Stiftungsrat Treuhand AG', '', 'Stiftungsrat', '03.09.2012', '01.01.2010', 'Kollektiv', '',
            ''],
           ['M-0099', 'Unknown', 'Person', 'Director', '01.01.2020', '', '', '', ''],
           ['M-0006', 'Alt', 'Peter', 'Geschäftsführer', '01.01.2010', '', '', '', ''],
           ['M-0004', 'Rahman', 'Aisha', 'Kassierin', '15.01.2021', '', '', 'AE', '']])

    lines = ['Mandat Nr;Name;Anteil;Erfasst am;Nationalität;Wohnland;Adresse',
             'M-0001;Muster, Hans;0.6;12.05.2011;Schweiz;Schweiz;Seestrasse 4, 6300 Zug',
             'M-0001;Keller, Anna;40%;12.05.2011;CH;Suisse;Rue du Lac 3, Genève',
             'M-0003;Zhang Wei;100;21.06.2019;CN;HK;',
             'Alpenblick Holding AG;Müller, Jürg;0;01.01.2020;DE;Österreich;',
             'Harbour Bridge;Li Na;10;;CN;CN;']
    (out / 'Wirtschaftlich Berechtigte.csv').write_bytes('\r\n'.join(lines).encode('cp1252'))

    sheet(out / 'Aktienbuch.xlsx',
          ['Mandat Nr', 'Name', 'Anzahl Aktien', 'Zertifikat Nr', 'Eintragung', 'Übertragen'],
          [['M-0001', 'Muster, Hans', "600 Namenaktien", 'Z-1', '12.05.2011', ''],
           ['M-0001', 'Keller, Anna', "400", 'Z-2', '12.05.2011', ''],
           ['M-0003', 'Zhang, Wei', '10,000 Ordinary', '1', '21.06.2019', '']])

    sheet(out / 'Bankverbindungen.xlsx',
          ['Mandat Nr', 'Bank', 'IBAN', 'BIC', 'Währung', 'Kontobezeichnung', 'Kontoinhaber'],
          [['M-0001', 'UBS Switzerland AG', 'CH93 0076 2011 6238 5295 7', 'UBSWCHZH80A', 'CHF', 'Kontokorrent', ''],
           ['M-0001', 'UBS Switzerland AG', 'CH12 0000 0000 0000 0000 0', 'UBSWCHZH80A', 'EUR', '', ''],
           ['M-0003', 'HSBC', '123-456789-001', 'HSBCHKHH', 'HKD', 'Current account', 'Harbour Bridge Trading Limited'],
           ['M-0004', 'Emirates NBD', 'AE070331234567890123456', 'EBILAEAD', '', '', '']])

    sheet(out / 'Honorare.xlsx',
          ['Mandat Nr', 'Leistungsart', 'Betrag', 'Währung', 'Periode', 'Nächste Fälligkeit', 'Optionaler Text'],
          [['M-0001', 'ABF', "5'000.00", 'CHF', 12, '01.03.2027', ''],
           ['M-0001', 'FLF', "2'500.00", 'CHF', '', '', 'Setup fee'],
           ['M-0003', 'ABF', '100.00', 'USD', 12, '21.06.2027', 'Compliance Fee Registered Agent']])

    items = ['Debitor Nr;Rechnung Nr;Rechnungsdatum;Fällig;Offen;Währung',
             "50001;R-2026-101;15.09.2026;15.10.2026;5'000.00;CHF",
             '50003;R-2026-102;01.10.2026;31.10.2026;1.250,50;USD',
             '50004;R-2026-103;01.10.2026;31.10.2026;800.00;AED',
             'Total;;;;7050.50;']
    (out / 'Offene Posten.csv').write_bytes('\r\n'.join(items).encode('cp1252'))

    sheet(out / 'Telefonliste Mitarbeiter.xlsx', ['Kürzel', 'Durchwahl', 'Raum'], [['PK', 201, 'A1'], ['MW', 202, 'A2']])
    (out / 'Lies mich.pdf').write_bytes(b'%PDF-1.4\n%sample\n')
    print(f"Sample M2 exports written to {out}")


def english(out):
    """M2 Professional with the English interface: the grid titles of Infosystem > Mandate info, exported per
    grid. The per-mandate grids (Detail addr., Bank, Fees) carry no mandate column: the mandate number is in the
    file name. Document store folders are named with the zero-padded mandate number (KP-00482-ACC)."""
    out.mkdir(parents=True, exist_ok=True)
    sheet(out / 'Mandate overview.xlsx',
          ['Mandate', 'Mandate desig. 1', 'CDB', 'SSB', 'Group', 'Mandate desig. 2', 'Mandate desig. 3', 'Active',
           'Status', 'Alpha', 'Lock serv.entry', 'Mad.Company', 'Debtor', 'Mandate start', '-end', 'Mandate form'],
          [['KP-482-ACC', 'Coral Reef Holdings Ltd.', 'FF', '', '', 'Marshall Islands', '', 'J', '', 'CORAL REEF',
            '', 'NOVA-PH', 21091, '18/06/2020', '', 'LTD'],
           ['KP-501-ACC', 'Tamarind Ventures Inc.', 'FF', 'JD', '', 'Belize', '', 'J', '', 'TAMARIND', '',
            'NOVA-PH', 21100, '02/03/2021', '', 'INC'],
           ['KP-502-TRU', 'Silver Fern Trust', 'JD', '', '', 'Cook Islands', '', 'N', '', 'SILVER FERN', '',
            'NOVA-PH', 21101, '01/01/2015', '', 'TRU'],
           ['KP-610-ACC', 'Baobab Trading Ltd.', 'JD', '', '', 'Seychelles', '', 'J', '', 'BAOBAB', '', 'ZZ-OTHER',
            21110, '05/05/2022', '', 'LTD']])
    sheet(out / 'Addresses.xlsx',
          ['Number', 'Salutation', 'Firma / Name', 'Given name', 'Firmazusatz', 'zHd', 'Street', 'Postfach',
           'Postcode, City', 'Country word', 'Language', 'Address type', 'Alpha', 'Phone 1', 'Phone 2', 'Fax',
           'eMail', 'Internet', 'Founded', 'Country of resid.', 'Business activity', 'Active'],
          [[21091, '', 'Coral Reef Holdings Ltd.', '', '', '', 'Trust Company Complex', 'Lagoon Road',
            'MH 96960 Ajeltake Island, Majuro', 'Marshall Islands', 'E', 'GES', 'CORAL REEF', '', '', '',
            'office@coralreef.example', '', '12/06/2020', '', 'Holding of investments', 'J'],
           [21100, '', 'Tamarind Ventures Inc.', '', '', 'Attn. Directors', '60 Market Square', '',
            'BZ Belize City', 'Belize', 'E', 'GES', 'TAMARIND', '+501 223 0000', '', '', 'info@tamarind.example', '',
            '', '', 'Consulting', 'J'],
           [21110, '', 'Baobab Trading Ltd.', '', '', '', 'Providence Industrial Estate', '', 'SC Mahé',
            'Seychelles', 'E', 'GES', 'BAOBAB', '', '', '', '', '', '', '', '', 'J']])
    sheet(out / 'KP-482-ACC Detail addr.xlsx',
          ['Type', 'Order', 'Adrno.', 'Firma / Name', 'Given name', 'collective', 'Remark', 'Remark2', 'Remark3',
           'Date from', 'Date till', 'collective with 1'],
          [['DIR', 1, 30010, 'Lim', 'Grace', 'N', 'Chair', '', '', '18/06/2020', '', ''],
           ['DIR', 2, 30011, 'Okafor', 'Daniel', 'J', '', 'Resident in Lagos', '', '18/06/2020', '', '30010'],
           ['BO', 1, 30010, 'Lim', 'Grace', '', '', '', '', '18/06/2020', '', ''],
           ['CP', 1, 30012, 'Ramos', 'Ana', '', 'Bookkeeping contact', '', '', '', '', ''],
           ['XQ', 1, 30013, 'Unknown Role', 'Pat', '', '', '', '', '', '', '']])
    sheet(out / 'KP-482-ACC Banks.xlsx',
          ['Type', 'Order', 'Address', 'Adrno.', 'Remark1', 'Date from', 'Date till', 'BCN', 'Mad.Company',
           'Paym.acc.no.'],
          [['BNK', 1, 'Pacific Coast Bank Ltd', 40001, 'Main bank', '18/06/2020', '', '8842', 'NOVA-PH', '']])
    sheet(out / 'KP-482-ACC Account.xlsx',
          ['Type', 'Order', 'Account type', 'Account no.', 'Remark 1'],
          [['KTO', 1, 'Current account USD', '001-778812-01', ''],
           ['KTO', 2, 'Call deposit', '001-778812-02', 'EUR']])
    sheet(out / 'KP-482-ACC Authorized to sign.xlsx',
          ['Type', 'Order', 'Address', 'Collectiv', 'Remark1', 'Address no.', 'Date', 'Date till'],
          [['SIG', 1, 'Lim, Grace', 'N', '', 30010, '18/06/2020', ''],
           ['SIG', 2, 'Okafor, Daniel', 'J', '', 30011, '18/06/2020', '']])
    sheet(out / 'KP-482-ACC Fees.xlsx',
          ['H.Type', 'Order', 'Due', 'Finish date', 'Deadline', 'Amount', 'Tax due date', 'Address', 'Firma / Name',
           'Given name', 'price currency', 'Index', 'Fee design.'],
          [['LEI', 1, '18/04/2027', '', 12, '250.00', '', 15739, 'Nova Services Inc', '', 'USD', 'N',
            'Annual renewal'],
           ['ABF', 1, '01/07/2027', '', 12, '1,500.00', '', 15739, 'Nova Services Inc', '', 'USD', 'N',
            'Annual basic fee'],
           ['FLF', 1, '01/07/2021', '31/12/2022', '', '300.00', '', 15739, 'Nova Services Inc', '', 'USD', 'N',
            'Old flat fee']])
    sheet(out / 'Last invoices.xlsx',
          ['Info', 'Invoice date', 'Status', 'PK no.', 'OP No.', 'Currency', 'Paid', 'Open amount domestic',
           'Open amount foreign', 'Total DC', 'Total FC', 'Invoice address', 'Debtor-Adr.No.', 'Level of reminder',
           'Last payment'],
          [['', '22/01/2026', 'Paid', 21091, 20260017, 'USD', 'J', 0, 0, 250, 250, 15680, 21091, 0, '30/01/2026'],
           ['', '09/10/2025', 'Paid', 21091, 20250368, 'USD', 'J', 0, 0, 250, 250, 15680, 21091, 0, '28/10/2025'],
           ['', '01/09/2026', 'Open', 21100, 20260301, 'USD', 'N', 1500, 1500, 1500, 1500, 15680, 21100, 1, ''],
           ['', '01/10/2026', 'Open', 21091, 20260350, 'USD', 'N', 250, 250, 250, 250, 15680, 21091, 0, '']])
    sheet(out / 'Compliance.xlsx',
          ['Mandate', 'ML-Related', 'Risk category', 'Country of max. risk', 'Lock', 'Lock text', 'Note'],
          [['KP-482-ACC', 'yes', '2 Average risk', 'Marshall Islands', 'N', '', 'Periodic review 2027'],
           ['KP-501-ACC', 'J', '3 High risk', 'Belize', 'J', 'Missing source of funds', '']])
    sheet(out / 'Documents.xlsx',
          ['Mandate', 'File name', 'Description', 'Creation date', 'Typ', 'Extension', 'Type', 'Maturity date'],
          [['KP-482-ACC', '20200168 2020-06-22.pdf', 'Invoice', '22/06/2020', 'D', 'PDF', 'DOKU', ''],
           ['KP-482-ACC', 'LEI certificate.pdf', 'LEI', '18/04/2026', 'D', 'PDF', 'DOKU', '18/04/2027']])
    for rel in ('KP-00482-ACC/Invoices PDF/20200168 2020-06-22.pdf', 'KP-00482-ACC/LEI/LEI certificate.pdf',
                'KP-00482-ACC/LEI/LEI application.pdf', 'Scans/unsorted.pdf'):
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_bytes(b'%PDF-1.4\n%sample\n')
    print(f"Sample M2 (English interface) exports written to {out}")


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--english':
        english(Path(sys.argv[2] if len(sys.argv) > 2 else Path(__file__).resolve().parent / 'sample_m2_en'))
    else:
        main()
