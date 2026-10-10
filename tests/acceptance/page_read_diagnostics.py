"""Bounded, value-free diagnostics; never used to accept inconsistent pages."""

from hashlib import sha256
import json
import re


def _encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str).encode()


def _integer(value):
    if type(value) is int and value>=0:
        return value
    if isinstance(value,str) and re.fullmatch(r'[0-9]{1,9}',value):
        return int(value)
    raise ValueError('invalid page integer')


class PageReadDiagnostics:
    def __init__(self,*,fields,keys,max_rows=100000):
        if (not isinstance(fields,tuple) or not 1<=len(fields)<=64 or len(set(fields))!=len(fields)
            or any(not isinstance(field,str) or re.fullmatch(r'[a-z][a-z0-9_]{0,63}',field) is None for field in fields)
            or not isinstance(keys,tuple) or not keys or not set(keys).issubset(fields)
            or type(max_rows) is not int or not 1<=max_rows<=100000):
            raise ValueError('invalid diagnostic configuration')
        self.fields,self.keys,self.max_rows=fields,keys,max_rows
        self.reads=[]
        self.invalid=False

    def observe(self,offset,rows):
        if self.invalid:
            return
        try:
            if type(offset) is not int or offset<0 or not isinstance(rows,list):
                raise ValueError('invalid page')
            if offset==0:
                if len(self.reads)==2:
                    raise ValueError('more than two reads')
                expected=_integer(rows[0]['group_count']) if rows else 0
                if expected>self.max_rows:
                    raise ValueError('diagnostic capacity')
                self.reads.append({'count':0,'expected':expected,'rows':[],'keys':[],
                    'fields':{field:sha256() for field in self.fields},'untracked':sha256()})
            if not self.reads:
                raise ValueError('missing first page')
            read=self.reads[-1]
            if offset!=read['count'] or offset+len(rows)>read['expected'] or (not rows and offset<read['expected']):
                raise ValueError('page offset or length')
            for row in rows:
                if (_integer(row['result_ordinal'])!=read['count']+1
                    or _integer(row['group_count'])!=read['expected']
                    or any(key not in row for key in self.keys)):
                    raise ValueError('page identity or controls')
                read['rows'].append(sha256(_encoded({k:v for k,v in row.items() if k!='result_ordinal'})).digest())
                read['keys'].append(sha256(_encoded([row[key] for key in self.keys])).digest())
                for field in self.fields:
                    read['fields'][field].update(sha256(_encoded((field in row,row.get(field)))).digest())
                read['untracked'].update(sha256(_encoded({k:v for k,v in row.items() if k not in self.fields and k!='result_ordinal'})).digest())
                read['count']+=1
        except (ValueError,TypeError,KeyError,ArithmeticError):
            self.invalid=True

    def summary(self):
        complete=not self.invalid and len(self.reads)==2 and all(read['count']==read['expected'] for read in self.reads)
        result={'two_complete_reads':complete,'invalid_page_evidence':self.invalid,
                'same_row_multiset':None,'same_key_multiset':None,'changed_fields':[],
                'untracked_content_changed':None}
        if complete:
            first,second=self.reads
            result.update(same_row_multiset=sorted(first['rows'])==sorted(second['rows']),
                same_key_multiset=sorted(first['keys'])==sorted(second['keys']),
                changed_fields=sorted(field for field in self.fields if first['fields'][field].digest()!=second['fields'][field].digest()),
                untracked_content_changed=first['untracked'].digest()!=second['untracked'].digest())
        return result
