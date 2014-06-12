"""
Create dummy submissions and assessments for testing.
"""
from uuid import uuid4
import copy
from django.core.management.base import BaseCommand, CommandError
import loremipsum
from submissions import api as sub_api
from openassessment.workflow import api as workflow_api
from openassessment.assessment.api import peer as peer_api
from openassessment.assessment.api import self as self_api

STEPS = ['peer', 'self']


class Command(BaseCommand):
    """
    Create dummy submissions and assessments for testing.
    This will generate fake (lorem ipsum) data for:
        * Submission response text
        * Assessment rubric definition
        * Assessment rubric scores
        * Assessment feedback
    """

    help = 'Create dummy submissions and assessments'
    args = '<COURSE_ID> <ITEM_ID> <NUM_SUBMISSIONS>'

    # Number of peer assessments to create per submission
    NUM_PEER_ASSESSMENTS = 3

    # Number of criteria / options in each rubric
    NUM_CRITERIA = 5
    NUM_OPTIONS = 5

    def __init__(self, *args, **kwargs):
        super(Command, self).__init__(*args, **kwargs)
        self._student_items = list()

    def handle(self, *args, **options):
        """
        Execute the command.

        Args:
            course_id (unicode): The ID of the course to create submissions for.
            item_id (unicode): The ID of the item in the course to create submissions for.
            num_submissions (int): Number of submissions to create.
        """
        if len(args) < 3:
            raise CommandError('Usage: create_oa_submissions <COURSE_ID> <ITEM_ID> <NUM_SUBMISSIONS>')

        course_id = unicode(args[0])
        item_id = unicode(args[1])

        try:
            num_submissions = int(args[2])
        except ValueError:
            raise CommandError('Number of submissions must be an integer')

        print u"Creating {num} submissions for {item} in {course}".format(
            num=num_submissions, item=item_id, course=course_id
        )

        for sub_num in range(num_submissions):

            print "Creating submission {num}".format(num=sub_num)

            # Create a dummy submission
            student_item = {
                'student_id': uuid4().hex[0:10],
                'course_id': course_id,
                'item_id': item_id,
                'item_type': 'openassessment'
            }
            submission_uuid = self._create_dummy_submission(student_item)
            self._student_items.append(student_item)

            # Create a dummy rubric
            rubric, options_selected = self._dummy_rubric()

            # Create peer assessments
            for num in range(self.NUM_PEER_ASSESSMENTS):
                print "-- Creating peer-assessment {num}".format(num=num)

                scorer_id = 'test_{num}'.format(num=num)

                # The scorer needs to make a submission before assessing
                scorer_student_item = copy.copy(student_item)
                scorer_student_item['student_id'] = scorer_id
                scorer_submission_uuid = self._create_dummy_submission(scorer_student_item)

                # Retrieve the submission we want to score
                # Note that we are NOT using the priority queue here, since we know
                # exactly which submission we want to score.
                peer_api.create_peer_workflow_item(scorer_submission_uuid, submission_uuid)

                # Create the peer assessment
                peer_api.create_assessment(
                    scorer_submission_uuid,
                    scorer_id,
                    options_selected, {}, "  ".join(loremipsum.get_paragraphs(2)),
                    rubric,
                    self.NUM_PEER_ASSESSMENTS
                )

            # Create a self-assessment
            print "-- Creating self assessment"
            self_api.create_assessment(
                submission_uuid, student_item['student_id'],
                options_selected, rubric
            )

    @property
    def student_items(self):
        """
        Return the list of student items created by the command.
        This is used for testing the command.

        Returns:
            list of serialized StudentItem models
        """
        return self._student_items

    def _create_dummy_submission(self, student_item):
        """
        Create a dummy submission for a student.

        Args:
            student_item (dict): Serialized StudentItem model.

        Returns:
            str: submission UUID
        """
        answer = {'text': "  ".join(loremipsum.get_paragraphs(5))}
        submission = sub_api.create_submission(student_item, answer)
        workflow_api.create_workflow(submission['uuid'], STEPS)
        workflow_api.update_from_assessments(
            submission['uuid'], {'peer': {'must_grade': 1, 'must_be_graded_by': 1}}
        )
        return submission['uuid']

    def _dummy_rubric(self):
        """
        Randomly generate a rubric and select options from it.

        Returns:
            rubric (dict)
            options_selected (dict)
        """
        rubric = {'criteria': list()}
        options_selected = dict()
        words = loremipsum.Generator().words

        for criteria_num in range(self.NUM_CRITERIA):
            criterion = {
                'name': words[criteria_num],
                'prompt': "  ".join(loremipsum.get_sentences(2)),
                'order_num': criteria_num,
                'options': list()
            }

            for option_num in range(self.NUM_OPTIONS):
                criterion['options'].append({
                    'order_num': option_num,
                    'points': option_num,
                    'name': words[option_num],
                    'explanation': "  ".join(loremipsum.get_sentences(2))
                })

            rubric['criteria'].append(criterion)
            options_selected[criterion['name']] = criterion['options'][0]['name']

        return rubric, options_selected